import os
import json
from flask import Flask, request, jsonify
from pymongo import MongoClient
from firebase_admin import credentials, initialize_app, messaging
from flask_cors import CORS
from datetime import datetime
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, filename='app.log', format='%(asctime)s - %(message)s')

# Firebase Admin SDK initialization
firebase_admin_sdk_json = os.environ.get("FIREBASE_ADMIN_SDK_JSON")
if firebase_admin_sdk_json:
    try:
        cred_dict = json.loads(firebase_admin_sdk_json)
        cred = credentials.Certificate(cred_dict)
        initialize_app(cred)
        logging.info("Firebase Admin SDK initialized successfully.")
    except Exception as e:
        logging.error(f"Error initializing Firebase Admin SDK: {e}")
else:
    logging.warning("FIREBASE_ADMIN_SDK_JSON environment variable not set.")

app = Flask(__name__)
CORS(app)

# MongoDB connection
mongo_uri = os.environ.get("MONGODB_URI")
if mongo_uri:
    try:
        client = MongoClient(mongo_uri)
        db = client.notifications_db
        tokens_collection = db.tokens
        notifications_history_collection = db.notifications_history
        logging.info("MongoDB connected successfully.")
    except Exception as e:
        logging.error(f"Error connecting to MongoDB: {e}")
        client = None # Ensure client is None if connection fails
else:
    logging.warning("MONGODB_URI environment variable not set.")
    client = None # Ensure client is None if URI is not set

@app.route("/")
def home():
    return "Pop Backend is running!"

@app.route("/api/register-token", methods=["POST"])
def register_token():
    if not client:
        return jsonify({"message": "Database not connected"}), 500
    data = request.get_json()
    token = data.get("token")
    if not token:
        return jsonify({"message": "Token is required"}), 400

    try:
        # Use upsert to update if exists, insert if not
        tokens_collection.update_one(
            {"token": token},
            {"$set": {"token": token, "timestamp": datetime.utcnow()}},
            upsert=True
        )
        return jsonify({"message": "Token registered successfully"}), 200
    except Exception as e:
        logging.error(f"Error registering token: {e}")
        return jsonify({"message": "Error registering token"}), 500

@app.route("/api/tokens")
def get_tokens():
    if not client:
        return jsonify({"message": "Database not connected"}), 500
    try:
        tokens = [doc["token"] for doc in tokens_collection.find({}, {"_id": 0, "token": 1})]
        return jsonify({"count": len(tokens), "tokens": tokens}), 200
    except Exception as e:
        logging.error(f"Error retrieving tokens: {e}")
        return jsonify({"message": "Error retrieving tokens"}), 500

@app.route("/api/db-health")
def db_health():
    if not client:
        return jsonify({"status": "MongoDB connection failed", "error": "MONGODB_URI not set or connection failed"}), 500
    try:
        client.admin.command("ping")
        return jsonify({"status": "MongoDB connected"}), 200
    except Exception as e:
        logging.error(f"MongoDB health check failed: {e}")
        return jsonify({"status": "MongoDB connection failed", "error": str(e)}), 500

@app.route("/api/send-notification", methods=["POST"])
def send_notification():
    if not client:
        return jsonify({"message": "Database not connected"}), 500
    data = request.get_json()
    title = data.get("title")
    body = data.get("body")
    image = data.get("image")

    if not title or not body:
        return jsonify({"message": "Title and body are required"}), 400

    try:
        all_tokens = [doc["token"] for doc in tokens_collection.find({}, {"_id": 0, "token": 1})]
    except Exception as e:
        logging.error(f"Error retrieving all tokens for sending notification: {e}")
        return jsonify({"message": "Error retrieving tokens for notification"}), 500

    if not all_tokens:
        return jsonify({"message": "No tokens registered"}), 404

    # Send messages in batches
    invalid_tokens = []
    success_count = 0
    failure_count = 0

    for i in range(0, len(all_tokens), 500):
        batch_tokens = all_tokens[i:i+500]
        messages = []
        for token in batch_tokens:
            message = messaging.Message(
                notification=messaging.Notification(title=title, body=body),
                token=token,
                webpush=messaging.WebpushConfig(
                    notification=messaging.WebpushNotification(image=image) if image else None
                )
            )
            messages.append(message)

        try:
            batch_response = messaging.send_all(messages)
            for idx, response in enumerate(batch_response.responses):
                if response.success:
                    success_count += 1
                else:
                    failure_count += 1
                    if response.exception and response.exception.code in [
                        "UNREGISTERED",
                        "INVALID_ARGUMENT",
                        "registration-token-not-registered"
                    ]:
                        invalid_tokens.append(batch_tokens[idx])
        except Exception as e:
            logging.error(f"Error sending batch: {e}")
            failure_count += len(batch_tokens) # Assume all in batch failed if exception

    if invalid_tokens:
        try:
            tokens_collection.delete_many({"token": {"$in": invalid_tokens}})
            logging.info(f"Removed {len(invalid_tokens)} invalid tokens.")
        except Exception as e:
            logging.error(f"Error removing invalid tokens: {e}")

    # Log notification to history
    try:
        notifications_history_collection.insert_one({
            "title": title,
            "body": body,
            "image": image,
            "timestamp": datetime.utcnow(),
            "total_tokens": len(all_tokens),
            "success_count": success_count,
            "failure_count": failure_count,
            "invalid_tokens_removed": len(invalid_tokens)
        })
        logging.info("Notification logged to history.")
    except Exception as e:
        logging.error(f"Error logging notification to history: {e}")

    return jsonify({
        "message": "Notification send process completed",
        "total_tokens_processed": len(all_tokens),
        "success_count": success_count,
        "failure_count": failure_count,
        "invalid_tokens_removed": len(invalid_tokens)
    }), 200

@app.route("/api/notifications-history")
def get_notifications_history():
    if not client:
        return jsonify({"message": "Database not connected"}), 500
    try:
        history = []
        for doc in notifications_history_collection.find({}, {"_id": 0}).sort("timestamp", -1):
            history.append(doc)
        return jsonify(history), 200
    except Exception as e:
        logging.error(f"Error retrieving notification history: {e}")
        return jsonify({"message": "Error retrieving notification history"}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=os.environ.get("PORT", 5000))


