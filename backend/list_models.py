"""Prints the model IDs your Groq key can use RIGHT NOW.

Run from the backend folder:   python list_models.py
"""
from app.llm import get_client

client = get_client()
models = sorted(m.id for m in client.models.list().data)
print(f"{len(models)} models available:\n")
for model_id in models:
    print(" ", model_id)
