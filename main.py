import os
import json
from flask import Flask, request, jsonify
from pymongo import MongoClient
from firebase_admin import credentials, initialize_app, messaging
from flask_cors import CORS

# Firebase Admin SDK initialization
cred_dict = json.loads(os.environ.get("FIREBASE_ADMIN_SDK_JSON"))
cred = credentials.Certificate(cred_dict)
initialize_app(cred)

app = Flask(__name__)
CORS(app)

# MongoDB connection
mongo_uri = os.environ.get("MONGODB_URI")
client = MongoClient(mongo_uri)
db = client.notifications_db
tokens_collection = db.tokens

@app.route('/')
def home():
    return 'Pop Backend is running!'

@app.route('/api/register-token', methods=['POST'])
def register_token():
    data = request.get_json()
    token = data.get('token')
    if not token:
        return jsonify({'message': 'Token is required'}), 400

    # Use upsert to update if exists, insert if not
    tokens_collection.update_one(
        {'token': token},
        {'$set': {'token': token, 'timestamp': datetime.utcnow()}},
        upsert=True
    )
    return jsonify({'message': 'Token registered successfully'}), 200

@app.route('/api/tokens')
def get_tokens():
    tokens = [doc['token'] for doc in tokens_collection.find({}, {'_id': 0, 'token': 1})]
    return jsonify({'count': len(tokens), 'tokens': tokens}), 200

@app.route('/api/db-health')
def db_health():
    try:
        client.admin.command('ping')
        return jsonify({'status': 'MongoDB connected'}), 200
    except Exception as e:
        return jsonify({'status': 'MongoDB connection failed', 'error': str(e)}), 500

@app.route('/api/send-notification', methods=['POST'])
def send_notification():
    data = request.get_json()
    title = data.get('title')
    body = data.get('body')
    image = data.get('image')

    if not title or not body:
        return jsonify({'message': 'Title and body are required'}), 400

    all_tokens = [doc['token'] for doc in tokens_collection.find({}, {'_id': 0, 'token': 1})]
    if not all_tokens:
        return jsonify({'message': 'No tokens registered'}), 404

    # Send messages in batches
    invalid_tokens = []
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
                if not response.success:
                    # Check for specific error codes that indicate invalid tokens
                    if response.exception and response.exception.code in [
                        'UNREGISTERED',
                        'INVALID_ARGUMENT',
                        'registration-token-not-registered'
                    ]:
                        invalid_tokens.append(batch_tokens[idx])
        except Exception as e:
            print(f"Error sending batch: {e}")

    if invalid_tokens:
        tokens_collection.delete_many({"token": {"$in": invalid_tokens}})
        return jsonify({
            'message': 'Notifications sent, some tokens removed due to invalidity',
            'invalid_tokens_count': len(invalid_tokens)
        }), 200
    else:
        return jsonify({'message': 'Notifications sent successfully'}), 200

if __name__ == '__main__':
    from datetime import datetime
    app.run(host='0.0.0.0', port=os.environ.get('PORT', 5000))

