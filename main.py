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
# Create a Flask app object. Cloud Run will automatically find and use this.
app = Flask(__name__)

# --- Logging Setup ---
# Set up basic configuration for logging
logging.basicConfig(level=logging.INFO)

# --- Firebase Initialization ---
# This code initializes the connection to our Firestore database.
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
    if mileage < 0:
        return "0-10000"
    start = (mileage // 10000) * 10000
    end = start + 10000
    return f"{start}-{end}"

def create_llm_prompt(data):
    """Creates a detailed, structured prompt for the Gemini LLM."""
    return f"""
    Please act as an expert car cost analyst. Based on the following vehicle data, provide a JSON object with estimated annual ownership costs.

    Vehicle Data:
    - Make: {data['make']}
    - Model: {data['model']}
    - Year: {data['year']}
    - Current Mileage: {data['mileage']}
    - Location (Zip Code): {data['zip_code']}
    - Expected Annual Mileage: {data['expected_annual_mileage']}

    Provide your response as a single, minified JSON object with NO additional text, explanations, or markdown. The JSON object should have the following structure and keys:
    {{
      "annual_fuel_cost": <number>,
      "annual_insurance": <number>,
      "annual_maintenance": <number>,
      "annual_repairs": <number>,
      "annual_taxes_fees": <number>,
      "five_year_depreciation": <number>
    }}

    For "annual_fuel_cost", consider if the vehicle is an EV and estimate charging costs, otherwise estimate gasoline costs based on the zip code's average fuel price and the car's typical MPG.
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

# --- Main Application Route ---
# Register the function to handle requests to the root URL ('/').
@app.route('/', methods=['POST', 'OPTIONS'])
def getCarCostEstimate():
    """
    HTTP Cloud Function to estimate car ownership costs.
    """
    if request.method == 'OPTIONS':
        return _build_cors_preflight_response()

    logging.info("Function execution started.")

    if db is None:
        logging.error("CRITICAL: Database client is not initialized.")
        error_response = jsonify({"error": "Internal Server Error: Database not initialized."})
        error_response.status_code = 500
        return _build_cors_actual_response(error_response)

    if request.method != 'POST':
        logging.warning(f"Invalid method received: {request.method}")
        error_response = jsonify({"error": "Method not allowed. Use POST."})
        error_response.status_code = 405
        return _build_cors_actual_response(error_response)

    request_json = request.get_json(silent=True)
    if not request_json:
        logging.warning("Invalid or missing JSON in request body.")
        error_response = jsonify({"error": "Invalid JSON."})
        error_response.status_code = 400
        return _build_cors_actual_response(error_response)

    required_fields = ["make", "model", "year", "mileage", "zip_code", "expected_annual_mileage"]
    for field in required_fields:
        if field not in request_json:
            logging.warning(f"Missing required field in request: '{field}'")
            error_response = jsonify({"error": f"Invalid request: '{field}' field is missing."})
            error_response.status_code = 400
            return _build_cors_actual_response(error_response)

    logging.info(f"Request validated successfully for: {request_json['make']} {request_json['model']}")

    make = str(request_json['make']).upper()
    model = str(request_json['model']).upper()
    year = int(request_json['year'])
    mileage = int(request_json['mileage'])
    zip_code = str(request_json['zip_code'])
    mileage_range = get_mileage_range(mileage)

    doc_id = f"{make}_{model}_{year}_{mileage_range}_{zip_code}"
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
                "make": request_json['make'],
                "model": request_json['model'],
                "year": request_json['year'],
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
