import base64
import json
import os
import re
import sys
import time
import uuid

from dotenv import load_dotenv
from google import genai
from google.genai import errors, types
from pydantic import BaseModel

import makers

load_dotenv()

MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
IMAGE_MODEL = os.getenv("GEMINI_IMAGE_MODEL", "gemini-3.1-flash-image")

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "generated")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ---------- Persistent memory ----------
# A small file that remembers facts about the user across restarts.
MEMORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory.json")

DEFAULT_MEMORY = {
    "user_name": "",       # e.g. "Alex"
    "facts": [],           # list of strings, e.g. ["Lives in Dhaka", "Prefers short answers"]
}

def _load_memory():
    try:
        with open(MEMORY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = dict(DEFAULT_MEMORY)
    # ensure keys exist
    for k, v in DEFAULT_MEMORY.items():
        data.setdefault(k, v)
    return data

def _save_memory(mem):
    try:
        with open(MEMORY_PATH, "w", encoding="utf-8") as f:
            json.dump(mem, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"   [could not save memory: {e}]")

MEMORY = _load_memory()

def _memory_block():
    """A short line to inject into the system prompt."""
    parts = []
    if MEMORY.get("user_name"):
        parts.append(f"The user's name is {MEMORY['user_name']}. Always address them by name when it feels natural.")
    for fact in MEMORY.get("facts", []):
        parts.append(f"- {fact}")
    if not parts:
        return ""
    return "What you remember about this user:\n" + "\n".join(parts)

def remember(key: str, value: str) -> str:
    """Remember a fact about the user for future conversations.

    Args:
        key: The thing to remember (e.g. 'name', 'location', 'preference').
        value: The value (e.g. 'Alex', 'Dhaka', 'loves short answers').

    Returns:
        A short confirmation message.
    """
    print(f"   [using tool: remember({key!r}, {value!r})]")
    key_l = (key or "").strip().lower()
    value = (value or "").strip()
    if not value:
        return "Error: nothing to remember — the value was empty."
    if key_l in ("name", "user name", "username", "first name", "full name"):
        MEMORY["user_name"] = value
        _save_memory(MEMORY)
        return f"Saved: the user's name is {value}. Acknowledge it warmly."
    # generic fact
    fact = f"{key.strip().capitalize()}: {value}"
    if fact not in MEMORY["facts"]:
        MEMORY["facts"].append(fact)
    _save_memory(MEMORY)
    return f"Saved: {fact}. Acknowledge it briefly."

# ---------- System prompt ----------
BASE_SYSTEM_PROMPT = """You are NeuraxisAI — a friendly, intelligent AI Assistant.

About you:
- Your name is NeuraxisAI.
- If the user asks "who are you?", "what is your name?", or anything similar, introduce yourself
  warmly as NeuraxisAI. Never claim to be Gemini, Google, or any other product.

Memory:
- You have a `remember` tool. Use it whenever the user shares a lasting fact about themselves
  — their name, where they live, what they do, their preferences, and so on.
  Call `remember(key="name", value="Alex")` when they tell you their name.
  Call `remember(key="location", value="Dhaka")` for where they live, and similar for other facts.
- If the user asks "what is my name?" and you don't see it in your memory block, say you don't
  know yet and ask them to tell you.

How you answer:
- Answer clearly and simply. Use short paragraphs or bullet points when it helps.
- If you are not sure about something, say so honestly.

File creation tools:
- generate_image: for pictures, logos, drawings.
- create_presentation: for slide decks.
- create_document: for Word documents, reports, essays.
After creating a file, do not repeat its content. Just say what you made in 1-2 friendly sentences.

You CANNOT create videos."""

def build_system_prompt():
    mem = _memory_block()
    if mem:
        return BASE_SYSTEM_PROMPT + "\n\n" + mem
    return BASE_SYSTEM_PROMPT

_client = None
tool_log = []

# ---------- Structured schemas ----------
class SlideSpec(BaseModel):
    title: str
    bullets: list[str]

class DeckSpec(BaseModel):
    title: str
    subtitle: str
    slides: list[SlideSpec]

class SectionSpec(BaseModel):
    heading: str
    paragraphs: list[str]
    bullets: list[str]

class DocSpec(BaseModel):
    title: str
    subtitle: str
    sections: list[SectionSpec]

# ---------- Helpers ----------
def get_client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    return _client

def _generate(contents, config=None, model=None, retries=3):
    for attempt in range(retries):
        try:
            return get_client().models.generate_content(
                model=model or MODEL, contents=contents, config=config
            )
        except errors.APIError as error:
            if error.code in (429, 500, 503) and attempt < retries - 1:
                time.sleep(4 * (attempt + 1))
                continue
            raise

def _structured(prompt, schema):
    response = _generate(
        prompt,
        types.GenerateContentConfig(response_mime_type="application/json", response_schema=schema),
    )
    parsed = getattr(response, "parsed", None)
    if parsed is not None:
        return parsed.model_dump() if hasattr(parsed, "model_dump") else dict(parsed)
    text = re.sub(r"^```(?:json)?|```$", "", (response.text or "").strip(), flags=re.M).strip()
    return json.loads(text)

def _new_file(base, extension):
    slug = re.sub(r"[^A-Za-z0-9]+", "-", base or "").strip("-")[:40] or "file"
    name = f"{slug}-{uuid.uuid4().hex[:6]}{extension}"
    return name, os.path.join(OUTPUT_DIR, name)

def _reason(error):
    message = str(error)
    if error.code == 429:
        return "the free quota is used up, or this feature is not included in the free plan"
    if error.code == 404:
        return "the model name was not found. Check GEMINI_MODEL / GEMINI_IMAGE_MODEL in the .env file"
    if error.code == 403 or "billing" in message.lower() or "free tier" in message.lower():
        return "this feature may need a paid plan"
    if error.code == 503:
        return "the AI service is busy right now"
    return message[:200]

# ---------- File-generation tools ----------
def generate_image(prompt: str) -> str:
    """Create an image from a text description and save it as a file for the user.

    Args:
        prompt: A detailed English description of the image.

    Returns:
        A message saying whether the image was created.
    """
    print(f"   [using tool: generate_image({prompt[:60]!r}...)]")
    try:
        response = _generate(
            prompt,
            types.GenerateContentConfig(response_modalities=["TEXT", "IMAGE"]),
            model=IMAGE_MODEL,
        )
        data, mime = None, "image/png"
        for candidate in response.candidates or []:
            content = getattr(candidate, "content", None)
            for part in (content.parts if content and content.parts else []):
                inline = getattr(part, "inline_data", None)
                if inline and inline.data:
                    data = inline.data
                    mime = inline.mime_type or mime
                    break
            if data:
                break
        if not data:
            return "Error: the image model did not return a picture."
        if isinstance(data, str):
            data = base64.b64decode(data)
        extension = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(mime, ".png")
        name, path = _new_file(prompt, extension)
        with open(path, "wb") as f:
            f.write(data)
    except errors.APIError as error:
        return f"Error: image creation failed because {_reason(error)}."
    except Exception as error:
        return f"Error: image creation failed ({error})."

    print(f"   [saved image: {path}]")
    tool_log.append({"tool": "Image", "file": {"kind": "image", "name": name,
                                                "url": f"/files/{name}", "title": prompt[:80]}})
    return f"Success: the image was created (file {name})."

def create_presentation(title: str, outline: str, slide_count: int = 6) -> str:
    """Create a PowerPoint slide deck (.pptx file) for the user."""
    print(f"   [using tool: create_presentation({title!r})]")
    slide_count = max(3, min(int(slide_count), 15))
    prompt = (
        "You are an expert presentation designer. Write the content for a slide deck.\n"
        f"Title: {title}\n"
        f"Number of content slides: exactly {slide_count}.\n"
        "Rules: short title (max 7 words), 3 to 5 bullets per slide.\n\n"
        f"Source material:\n{outline}"
    )
    try:
        data = _structured(prompt, DeckSpec)
        name, path = _new_file(title, ".pptx")
        count = makers.build_presentation(data, path)
    except errors.APIError as error:
        return f"Error: the presentation could not be created because {_reason(error)}."
    except Exception as error:
        return f"Error: the presentation could not be created ({error})."

    print(f"   [saved presentation: {path}]")
    tool_log.append({"tool": "Slides", "file": {"kind": "slides", "name": name,
                                                "url": f"/files/{name}",
                                                "title": data.get("title") or title, "count": count}})
    return f"Success: a {count}-slide presentation was created."

def create_document(title: str, content: str) -> str:
    """Create a Word document (.docx file) for the user."""
    print(f"   [using tool: create_document({title!r})]")
    prompt = (
        "You are an expert writer. Write a clear, well-structured document.\n"
        f"Title: {title}\n"
        "Rules: short subtitle, then 3 to 8 sections with headings, paragraphs and optional bullets.\n\n"
        f"Source material and requirements:\n{content}"
    )
    try:
        data = _structured(prompt, DocSpec)
        name, path = _new_file(title, ".docx")
        makers.build_document(data, path)
    except errors.APIError as error:
        return f"Error: the document could not be created because {_reason(error)}."
    except Exception as error:
        return f"Error: the document could not be created ({error})."

    print(f"   [saved document: {path}]")
    tool_log.append({"tool": "Document", "file": {"kind": "document", "name": name,
                                                  "url": f"/files/{name}",
                                                  "title": data.get("title") or title}})
    return "Success: the Word document was created."

# ---------- The agent ----------
def create_chat(history=None):
    """Create a fresh Gemini chat. If `history` is provided (a list of
    {'role': 'user'|'model', 'text': ...} dicts), replay it so the model
    remembers the whole conversation.
    """
    chat = get_client().chats.create(
        model=MODEL,
        config=types.GenerateContentConfig(
            system_instruction=build_system_prompt(),
            tools=[
                remember,             # ← our new memory tool
                generate_image,
                create_presentation,
                create_document,
            ],
        ),
    )
    if history:
        for turn in history:
            role = turn.get("role")
            text = turn.get("text") or ""
            if not text.strip():
                continue
            try:
                if role == "user":
                    chat.send_message(text)
                elif role == "model":
                    # Replay assistant messages as chat history.
                    # The SDK accepts a plain text part with role='model'.
                    chat.send_message(text)  # simple round-trip; keeps memory, minor token cost
            except Exception as e:
                print(f"   [replay warning: {e}]")
    return chat

def ask(chat, question, retries=4):
    for attempt in range(retries):
        try:
            response = chat.send_message(question)
            return response.text or "(no answer)"
        except errors.APIError as error:
            busy = error.code in (429, 500, 503)
            if busy and attempt < retries - 1:
                wait = 5 * (attempt + 1)
                print(f"   [server busy, trying again in {wait} seconds...]")
                time.sleep(wait)
                continue
            raise

def ask_with_tools(chat, question):
    tool_log.clear()
    answer = ask(chat, question)
    return answer, list(tool_log)

def main():
    if not os.getenv("GEMINI_API_KEY"):
        print("No API key found. Create a .env file with: GEMINI_API_KEY=your-key")
        sys.exit(1)

    print("=" * 40)
    print("        NEURAXISAI (Gemini)")
    print("=" * 40)
    print("Type 'quit' to exit. Files you create are saved in the 'generated' folder.\n")

    chat = create_chat()

    while True:
        question = input("You: ").strip()
        if not question:
            continue
        if question.lower() in ("quit", "exit"):
            print("Goodbye!")
            break
        try:
            print(f"\nNeuraxisAI: {ask(chat, question)}\n")
        except errors.APIError as error:
            print(f"\nAPI error: {error}\n")

if __name__ == "__main__":
    main()