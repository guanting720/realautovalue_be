# main.py
# This is the Python code for our Google Cloud Function.
# It will be triggered by an HTTP POST request.

import firebase_admin
from firebase_admin import credentials, firestore
import google.cloud.firestore
import datetime
import requests
import json
import os
from flask import Flask, request, jsonify, Response
import logging

# --- Flask App Initialization ---
app = Flask(__name__)

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO)

# --- Firebase Initialization ---
try:
    if not firebase_admin._apps:
        firebase_admin.initialize_app()
    db = firestore.client()
    logging.info("Firestore client initialized successfully.")
except Exception as e:
    logging.error(f"Error initializing Firestore client: {e}", exc_info=True)
    db = None

# --- Configuration ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
CACHE_EXPIRATION_DAYS = 180

def get_mileage_range(mileage):
    """Categorizes mileage into 10,000-mile ranges for better caching."""
    if mileage < 0: return "0-10000"
    start = (mileage // 10000) * 10000
    end = start + 10000
    return f"{start}-{end}"

def create_llm_prompt(data):
    """Creates a detailed, structured prompt for the Gemini LLM."""
    trim_text = data.get('trim', '').strip()
    if trim_text:
        trim_info = f"- Trim: {trim_text}"
    else:
        trim_info = "- Trim: Not specified. Please use a popular or base trim for this model in your estimation."

    return f"""
    Please act as an expert car cost analyst. Based on the following vehicle data, provide a JSON object with estimated annual ownership costs.

    Vehicle Data:
    - Year: {data['year']}
    - Make: {data['make']}
    - Model: {data['model']}
    {trim_info}
    - Current Mileage: {data['mileage']}
    - Location (Zip Code): {data['zip_code']}
    - Expected Annual Mileage: {data['expected_annual_mileage']}

    Provide your response as a single, minified JSON object with NO additional text, explanations, or markdown. The JSON object must have the following structure and keys:
    {{
      "annual_fuel_cost": <number>,
      "annual_insurance": <number>,
      "annual_routine_maintenance": <number>,
      "annual_wear_and_tear_cost": <number>,
      "annual_repairs": <number>,
      "reliability_score": <number>,
      "annual_taxes_fees": <number>,
      "depreciation_percentages": [<number>, <number>, <number>, <number>, <number>]
    }}

    - For "annual_routine_maintenance", estimate the cost for standard services like oil changes, tire rotations, air filter changes, and inspections.
    - For "annual_wear_and_tear_cost", estimate the prorated annual average cost for parts that wear out on different schedules, such as tires, brakes, battery, and wiper blades.
    - For "annual_repairs", estimate unexpected mechanical or electrical repair costs. This value should be influenced by the reliability_score, the general cost of parts for the make, and the complexity of the vehicle's systems.
    - For "reliability_score", provide an integer score from 1 (very unreliable) to 10 (very reliable) for this specific year, make, and model.
    - For "depreciation_percentages", provide an array of 5 numbers, where each number is the percentage of the car's current value lost for each of the next 5 years.
    - For "annual_taxes_fees", include estimates for annual registration, title, and other state or local fees.
    """

def _build_cors_preflight_response():
    """Builds a CORS preflight response."""
    response = Response()
    response.headers.add("Access-Control-Allow-Origin", "*")
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'POST, OPTIONS')
    response.status_code = 204
    return response

def _build_cors_actual_response(response):
    """Builds a CORS actual response."""
    response.headers.add("Access-Control-Allow-Origin", "*")
    return response

@app.route('/', methods=['POST', 'OPTIONS'])
def getCarCostEstimate():
    """HTTP Cloud Function to estimate car ownership costs."""
    if request.method == 'OPTIONS':
        return _build_cors_preflight_response()

    logging.info("Function execution started.")

    if db is None:
        logging.error("CRITICAL: Database client is not initialized.")
        error_response = jsonify({"error": "Internal Server Error: Database not initialized."})
        error_response.status_code = 500
        return _build_cors_actual_response(error_response)

    request_json = request.get_json(silent=True)
    if not request_json:
        logging.warning("Invalid or missing JSON in request body.")
        error_response = jsonify({"error": "Invalid JSON."})
        error_response.status_code = 400
        return _build_cors_actual_response(error_response)

    required_fields = ["year", "make", "model", "mileage", "zip_code", "expected_annual_mileage"]
    for field in required_fields:
        if field not in request_json:
            logging.warning(f"Missing required field in request: '{field}'")
            error_response = jsonify({"error": f"Invalid request: '{field}' field is missing."})
            error_response.status_code = 400
            return _build_cors_actual_response(error_response)

    logging.info(f"Request validated successfully for: {request_json['year']} {request_json['make']} {request_json['model']}")

    year = int(request_json['year'])
    make = str(request_json['make']).upper().replace(' ', '')
    model = str(request_json['model']).upper().replace(' ', '')
    trim = str(request_json.get('trim', '')).upper().replace(' ', '')
    mileage = int(request_json['mileage'])
    zip_code = str(request_json['zip_code'])
    mileage_range = get_mileage_range(mileage)

    if trim:
        doc_id = f"{year}_{make}_{model}_{trim}_{mileage_range}_{zip_code}"
    else:
        doc_id = f"{year}_{make}_{model}_{mileage_range}_{zip_code}"
    
    doc_ref = db.collection('car_cost_estimates').document(doc_id)
    logging.info(f"Checking cache for document ID: {doc_id}")

    try:
        doc = doc_ref.get()
        if doc.exists:
            data = doc.to_dict()
            last_updated = data['metadata']['last_updated']
            if last_updated.tzinfo is None:
                last_updated = last_updated.replace(tzinfo=datetime.timezone.utc)

            if (datetime.datetime.now(datetime.timezone.utc) - last_updated).days < CACHE_EXPIRATION_DAYS:
                logging.info(f"Cache HIT for document: {doc_id}")
                data['source'] = 'cache'
                data['metadata']['last_updated'] = last_updated.isoformat()
                success_response = jsonify(data)
                success_response.status_code = 200
                return _build_cors_actual_response(success_response)

    except Exception as e:
        logging.warning(f"Error accessing Firestore cache, proceeding to LLM. Error: {e}", exc_info=True)
        pass

    logging.info(f"Cache MISS for document: {doc_id}. Calling LLM.")

    try:
        prompt = create_llm_prompt(request_json)
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"response_mime_type": "application/json"}
        }

        response = requests.post(GEMINI_API_URL, json=payload, headers={'Content-Type': 'application/json'})
        response.raise_for_status()

        llm_response_text = response.json()['candidates'][0]['content']['parts'][0]['text']
        estimates = json.loads(llm_response_text)
        logging.info("Successfully received and parsed response from LLM.")
        
        current_time_utc = datetime.datetime.now(datetime.timezone.utc)
        response_data = {
            "source": "live_llm",
            "estimates": estimates,
            "metadata": {
                "year": request_json['year'],
                "make": request_json['make'],
                "model": request_json['model'],
                "trim": request_json.get('trim', ''),
                "last_updated": current_time_utc
            }
        }
        
        db.collection('car_cost_estimates').document(doc_id).set(response_data)
        logging.info(f"Successfully cached data for document: {doc_id}")

        response_data['metadata']['last_updated'] = current_time_utc.isoformat()
        
        success_response = jsonify(response_data)
        success_response.status_code = 200
        return _build_cors_actual_response(success_response)

    except Exception as e:
        logging.error(f"An unexpected error occurred during LLM call or processing: {e}", exc_info=True)
        error_response = jsonify({"error": "An internal server error occurred."})
        error_response.status_code = 500
        return _build_cors_actual_response(error_response)
