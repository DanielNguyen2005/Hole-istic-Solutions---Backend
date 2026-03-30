from azure.storage.blob import BlobServiceClient, ContentSettings
import requests
import uuid
from flask import Flask, request, jsonify
from flask_cors import CORS
import pyodbc
import os
from datetime import datetime
from dotenv import load_dotenv

# Load the passwords from the .env file
load_dotenv()

app = Flask(__name__)
CORS(app)
# --- DATABASE CONNECTION LOGIC ---
def get_db_connection():
    server = os.getenv('DB_SERVER')
    database = os.getenv('DB_NAME')
    username = os.getenv('DB_USER')
    password = os.getenv('DB_PASS')
    driver = '{ODBC Driver 17 for SQL Server}' # Standard Azure driver

    conn_str = f'DRIVER={driver};SERVER={server};PORT=1433;DATABASE={database};UID={username};PWD={password}'
    return pyodbc.connect(conn_str)

# --- CLOUD STORAGE LOGIC ---
def upload_to_blob(file):
    try:
        connect_str = os.getenv('AZURE_STORAGE_CONNECTION_STRING')
        blob_service_client = BlobServiceClient.from_connection_string(connect_str)
        
        # We rename the file with a random UUID so two people uploading "pothole.jpg" don't overwrite each other
        filename = str(uuid.uuid4()) + "_" + file.filename
        blob_client = blob_service_client.get_blob_client(container="images", blob=filename)
        
        # Push the physical file into the Azure cloud
        image_settings = ContentSettings(content_type=file.content_type)
        blob_client.upload_blob(file, content_settings=image_settings)
        
        # Return the permanent public web address of the image!
        return blob_client.url
    except Exception as e:
        print(f"Blob upload error: {e}")
        return None

# --- AI VERIFICATION LOGIC ---
def verify_pothole_with_ai(image_url):
    try:
        # 1. Grab Sean's keys from the .env file
        endpoint = os.getenv('AI_ENDPOINT')
        project_id = os.getenv('AI_PROJECT_ID')
        iteration_name = os.getenv('AI_ITERATION_NAME') # Sean needs to tell you this! (e.g., "Iteration1")
        ai_key = os.getenv('AI_PREDICTION_KEY')
        
        # 2. Build the official Azure Custom Vision Prediction URL
        prediction_url = f"{endpoint}customvision/v3.0/Prediction/{project_id}/detect/iterations/{iteration_name}/url"
        
        headers = {
            'Prediction-Key': ai_key,
            'Content-Type': 'application/json'
        }
        
        # 3. Send the image we just saved to Blob Storage over to the AI
        body = {"url": image_url}
        response = requests.post(prediction_url, headers=headers, json=body)
        
        if response.status_code != 200:
            print("AI API Error:", response.text)
            return True # Fallback: let it through if Sean's AI is offline
            
        data = response.json()
        
        # 4. Check the AI's math! Look for 'pothole' with > 60% confidence
        for prediction in data.get('predictions', []):
            if 'pothole' in prediction['tagName'].lower() and prediction['probability'] > 0.60:
                print(f"AI Approved! Detected '{prediction['tagName']}' with Confidence: {prediction['probability'] * 100:.2f}%")
                return True 
                
        print("AI Rejected: No pothole detected.")
        return False
        
    except Exception as e:
        print(f"AI Connection Error: {e}")
        return True

# --- CREATE API ENDPOINT ---
@app.route('/api/report', methods=['POST'])
def receive_report():
    data = request.form

    # 1. Check for missing required fields
    required_fields = ['device_id', 'lat', 'lon', 'severity']
    for field in required_fields:
        if field not in data:
            return jsonify({"error": f"Missing required field: {field}"}), 400

    try:
        lat = float(data['lat'])
        lon = float(data['lon'])
    except KeyError:
        return jsonify({"error": "Location object must contain latitude and longitude"}), 400

    if not (50.8 <= lat <= 51.2):
        return jsonify({"error": "Latitude is outside Calgary city limits. Report rejected."}), 400
        
    if not (-114.3 <= lon <= -113.8):
        return jsonify({"error": "Longitude is outside Calgary city limits. Report rejected."}), 400

    # 3. Validate Severity Range (assuming a 1-10 scale)
    severity = int(data['severity'])
    if not (1 <= severity <= 10):
        return jsonify({"error": "Severity must be between 1 and 10."}), 400

    # --- NEW IMAGE UPLOAD LOGIC ---
    # 1. Check if the frontend actually attached a file named 'image'
    if 'image' not in request.files:
        return jsonify({"error": "No image file attached."}), 400
            
    file = request.files['image']

    # 1. Grab the file from the React waiter
    file = request.files['image']
        
    # 2. Upload it to Azure and save the link as 'image_url'
    image_url = upload_to_blob(file)
    
    # 3. Stop if the upload failed
    if not image_url:
        return jsonify({"error": "Failed to save image to Azure Cloud."}), 500
        
    # 2. Push the physical file into your Azure Blob container
    is_pothole = verify_pothole_with_ai(image_url)
        
    # 3. If the upload fails, stop the report and throw an error
    if not is_pothole:
       return jsonify({"error": "AI rejected report: No pothole detected in image."}), 400
    
    pothole_size = data.get('size', 'small').lower()
    traffic_volume = data.get('traffic_volume', 'low').lower()

    base_score = severity * 10  

    size_multiplier = 1.0
    if pothole_size == 'medium':
        size_multiplier = 1.2
    elif pothole_size == 'large':
        size_multiplier = 1.5

    # Apply Traffic Multiplier
    traffic_multiplier = 1.0
    if traffic_volume == 'medium':
        traffic_multiplier = 1.2
    elif traffic_volume == 'high':
        traffic_multiplier = 1.5

    final_score = base_score * size_multiplier * traffic_multiplier

    if final_score >= 100:
        urgency_tier = 'Critical'
    elif final_score >= 60:
        urgency_tier = 'Urgent'
    else:
        urgency_tier = 'Normal'
        
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        insert_query = """
            INSERT INTO Reports (device_id, submitted_at, latitude, longitude, image_url, severity, urgency_tier)
            VALUES (?, GETDATE(), ?, ?, ?, ?, ?)
        """
        cursor.execute(insert_query, data['device_id'], lat, lon, image_url, severity, urgency_tier)
        
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Database error: {e}")
        return jsonify({"error": "Failed to save to database"}), 500

    # 6. Success Message
    return jsonify({
        "message": "Report successfully received, scored, and saved to Azure!",
        "calculated_tier": urgency_tier
    }), 201

# --- GET API ENDPOINT (Send data to the Frontend) ---
@app.route('/api/reports', methods=['GET'])
def get_reports():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Pull everything from Azure and sort it by Urgency (Critical first)
        select_query = """
            SELECT id, device_id, submitted_at, latitude, longitude, image_url, severity, urgency_tier
            FROM Reports
            ORDER BY 
                CASE urgency_tier
                    WHEN 'Critical' THEN 1
                    WHEN 'Urgent' THEN 2
                    WHEN 'Normal' THEN 3
                    ELSE 4
                END,
                severity DESC
        """
        cursor.execute(select_query)
        rows = cursor.fetchall()
        
        # Package the raw SQL data into a clean JSON list for Shar
        reports_list = []
        for row in rows:
            reports_list.append({
                "id": row.id,
                "device_id": row.device_id,
                "submitted_at": row.submitted_at,
                "latitude": float(row.latitude),
                "longitude": float(row.longitude),
                "image_url": row.image_url,
                "severity": row.severity,
                "urgency_tier": row.urgency_tier
            })
            
        cursor.close()
        conn.close()
        
        return jsonify(reports_list), 200
        
    except Exception as e:
        print(f"Database fetch error: {e}")
        return jsonify({"error": "Failed to retrieve reports from database"}), 500
    
# --- DASHBOARD ENDPOINT: GET ALL REPORTS ---
@app.route('/api/reports', methods=['GET'])
def get_all_reports():
    try:
        # 1. Open the vault
        conn = get_db_connection()
        cursor = conn.cursor()

        # 2. Grab all potholes, sorting the most urgent ones to the very top!
        cursor.execute("SELECT * FROM Reports ORDER BY urgency_tier DESC")
        rows = cursor.fetchall()

        # 3. Translate the SQL data into a clean Python list
        reports_list = []
        for row in rows:
            reports_list.append({
                "id": row.id,
                "device_id": row.device_id,
                "latitude": float(row.latitude),
                "longitude": float(row.longitude),
                "severity": row.severity,
                "urgency_tier": row.urgency_tier,
                "image_url": row.image_url
            })

        conn.close()

        # 4. Ship it to the frontend
        return jsonify(reports_list), 200

    except Exception as e:
        print("GET Error:", e)
        return jsonify({"error": "Could not fetch dashboard data"}), 500
    
# Start the server
if __name__ == '__main__':
    app.run(debug=True, port=5000)