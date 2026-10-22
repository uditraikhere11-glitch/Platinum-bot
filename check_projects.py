import os
import json
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

with open(".env", "r") as f:
    vertex_creds_env = None
    for line in f:
        if line.startswith("VERTEX_CREDENTIALS_JSON="):
            vertex_creds_env = line.strip().split("=", 1)[1]

if not vertex_creds_env:
    print("VERTEX_CREDENTIALS_JSON not found in .env")
    exit(1)

creds_info = json.loads(vertex_creds_env)
creds = Credentials.from_authorized_user_info(creds_info)

try:
    rm = build('cloudresourcemanager', 'v1', credentials=creds)
    projects = rm.projects().list().execute()
    
    print("Found the following Google Cloud Projects for your account:")
    for p in projects.get('projects', []):
        if p.get('lifecycleState') == 'ACTIVE':
            print(f"- Name: {p.get('name')} | ID: {p.get('projectId')}")
            
except Exception as e:
    print(f"Error checking projects: {e}")
