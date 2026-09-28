# test_gemini.py — local smoke test; reads GEMINI_API_KEY from .env
import os

from dotenv import load_dotenv
from google import genai

load_dotenv()

api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    raise SystemExit("Set GEMINI_API_KEY in .env before running this script.")

model = os.getenv("GEMINI_MODEL_FLASH", "gemini-3.1-flash-lite")

client = genai.Client(api_key=api_key)
interaction = client.interactions.create(
    model=model,
    input="Say hello in one word.",
)
print(interaction.output_text)
