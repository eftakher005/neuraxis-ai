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

# ============================================================
#  PER-USER MEMORY
#  memory.json stores one profile per user_id:
#     {
#       "u-abc123": {"user_name": "Alex", "facts": ["Lives in Dhaka"]},
#       "u-xyz789": {"user_name": "Sam",  "facts": []}
#     }
# ============================================================
MEMORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory.json")

DEFAULT_USER_MEMORY = {"user_name": "", "facts": []}
_memory_cache = None


def _load_memory():
    global _memory_cache
    if _memory_cache is not None:
        return _memory_cache
    try:
        with open(MEMORY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    _memory_cache = data
    return _memory_cache


def _save_memory():
    global _memory_cache
    try:
        with open(MEMORY_PATH, "w", encoding="utf-8") as f:
            json.dump(_memory_cache or {}, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"   [could not save memory: {e}]")


def get_user_memory(user_id):
    """Return the profile dict for a user, creating it if needed."""
    mem = _load_memory()
    uid = (user_id or "anonymous").strip() or "anonymous"
    if uid not in mem:
        mem[uid] = dict(DEFAULT_USER_MEMORY)
        _save_memory()
    for k, v in DEFAULT_USER_MEMORY.items():
        mem[uid].setdefault(k, v)
    return mem[uid]


def update_user_memory(user_id, key, value):
    """Save one fact for a specific user."""
    profile = get_user_memory(user_id)
    key_l = (key or "").strip().lower()
    value = (value or "").strip()
    if not value:
        return "Error: nothing to remember — the value was empty."
    if key_l in ("name", "user name", "username", "first name", "full name"):
        profile["user_name"] = value
        _save_memory()
        return f"Saved: the user's name is {value}. Acknowledge it warmly."
    fact = f"{key.strip().capitalize()}: {value}"
    if fact not in profile["facts"]:
        profile["facts"].append(fact)
    _save_memory()
    return f"Saved: {fact}. Acknowledge it briefly."


def _memory_block_for(user_id):
    """Text to inject into the system prompt for this user."""
    profile = get_user_memory(user_id)
    parts = []
    if profile.get("user_name"):
        parts.append(f"The user's name is {profile['user_name']}. "
                     f"Always address them by name when it feels natural.")
    for fact in profile.get("facts", []):
        parts.append(f"- {fact}")
    if not parts:
        return ""
    return "What you remember about this user:\n" + "\n".join(parts)


# ---------- System prompt ----------
BASE_SYSTEM_PROMPT = """You are NeuraxisAI — a friendly, intelligent AI research assistant.

About you:
- Your name is NeuraxisAI.
- If the user asks "who are you?", "what is your name?", or anything similar, introduce yourself
  warmly as NeuraxisAI and briefly describe what you can help with. Never claim to be Gemini,
  Google, or any other product. You are NeuraxisAI.

Memory:
- You have a `remember` tool. Use it whenever the user shares a lasting fact about themselves
  — their name, where they live, what they do, their preferences, and so on.
  Call `remember(key="name", value="Alex")` when they tell you their name.
  Call `remember(key="location", value="Dhaka")` for where they live, and similar for other facts.
- If the user asks "what is my name?" and you don't see it in your memory block, say you don't
  know yet and ask them to tell you.

How you answer:
- Answer clearly and simply. Use short paragraphs or bullet points when it helps.
- You have access to Google Search when it is enabled by the environment. When the user asks
  about current events, recent news, live information, current office holders, sports scores,
  weather, prices, or anything that might have changed recently, prefer fresh results over
  your own training data.
- If you are not sure about something, say so honestly.

File creation tools:
- generate_image: for pictures, logos, drawings.
- create_presentation: for slide decks.
- create_document: for Word documents, reports, essays.
After creating a file, do not repeat its content. Just say what you made in 1-2 friendly sentences.

You CANNOT create videos in this app. If asked for a video, say so honestly and offer a video script,
a storyboard, or a slide deck instead."""


def build_system_prompt(user_id):
    mem = _memory_block_for(user_id)
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


# ---------- Tools ----------
def generate_image(prompt: str) -> str:
    """Create an image from a text description and save it as a file for the user.

    Args:
        prompt: A detailed English description of the image (subject, style, colors, lighting, mood).

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
            return ("Error: the image model did not return a picture. It may have refused the description. "
                    "Tell the user and suggest a different description.")
        if isinstance(data, str):
            data = base64.b64decode(data)
        extension = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(mime, ".png")
        name, path = _new_file(prompt, extension)
        with open(path, "wb") as f:
            f.write(data)
    except errors.APIError as error:
        return f"Error: image creation failed because {_reason(error)}. Explain this simply to the user."
    except Exception as error:
        return f"Error: image creation failed ({error}). Explain this simply to the user."

    print(f"   [saved image: {path}]")
    tool_log.append({"tool": "Image", "file": {"kind": "image", "name": name, "url": f"/files/{name}", "title": prompt[:80]}})
    return f"Success: the image was created and the user can see it below your message (file {name})."


def create_presentation(title: str, outline: str, slide_count: int = 6) -> str:
    """Create a PowerPoint slide deck (.pptx file) for the user.

    Args:
        title: The title of the presentation.
        outline: All the content, facts and key points the slides should cover.
        slide_count: How many content slides to make (3 to 15).

    Returns:
        A message saying whether the presentation was created.
    """
    print(f"   [using tool: create_presentation({title!r})]")
    slide_count = max(3, min(int(slide_count), 15))
    prompt = (
        "You are an expert presentation designer. Write the content for a slide deck.\n"
        f"Title: {title}\n"
        f"Number of content slides: exactly {slide_count} (do not count the title slide).\n"
        "Rules: every slide has a short title (max 7 words) and 3 to 5 bullets. Each bullet is one short, "
        "clear phrase or sentence (max 18 words). Do not use markdown symbols. 'subtitle' is one short line. "
        "Write in the same language as the source material.\n\n"
        f"Source material:\n{outline}"
    )
    try:
        data = _structured(prompt, DeckSpec)
        name, path = _new_file(title, ".pptx")
        count = makers.build_presentation(data, path)
    except errors.APIError as error:
        return f"Error: the presentation could not be created because {_reason(error)}. Explain this simply to the user."
    except Exception as error:
        return f"Error: the presentation could not be created ({error}). Explain this simply to the user."

    print(f"   [saved presentation: {path}]")
    tool_log.append({"tool": "Slides", "file": {"kind": "slides", "name": name, "url": f"/files/{name}",
                                                "title": data.get("title") or title, "count": count}})
    return f"Success: a {count}-slide presentation was created and the user can download it below your message."


def create_document(title: str, content: str) -> str:
    """Create a Word document (.docx file) for the user, such as a report, essay or letter.

    Args:
        title: The title of the document.
        content: All the information, ideas and requirements the document should cover.

    Returns:
        A message saying whether the document was created.
    """
    print(f"   [using tool: create_document({title!r})]")
    prompt = (
        "You are an expert writer. Write a clear, well-structured document.\n"
        f"Title: {title}\n"
        "Rules: give a short subtitle, then 3 to 8 sections. Each section has a heading, 1 to 3 paragraphs "
        "(2 to 5 sentences each) and optionally bullet points (use an empty list if none are needed). "
        "Do not use markdown symbols. Write in the same language as the source material.\n\n"
        f"Source material and requirements:\n{content}"
    )
    try:
        data = _structured(prompt, DocSpec)
        name, path = _new_file(title, ".docx")
        makers.build_document(data, path)
    except errors.APIError as error:
        return f"Error: the document could not be created because {_reason(error)}. Explain this simply to the user."
    except Exception as error:
        return f"Error: the document could not be created ({error}). Explain this simply to the user."

    print(f"   [saved document: {path}]")
    tool_log.append({"tool": "Document", "file": {"kind": "document", "name": name, "url": f"/files/{name}",
                                                  "title": data.get("title") or title}})
    return "Success: the Word document was created and the user can download it below your message."


# ---------- The agent ----------
def _make_remember_tool(user_id):
    """Build a remember() function bound to a specific user."""
    def remember(key: str, value: str) -> str:
        """Remember a fact about the current user for future conversations.

        Args:
            key: The thing to remember (e.g. 'name', 'location', 'preference').
            value: The value (e.g. 'Alex', 'Dhaka', 'loves short answers').

        Returns:
            A short confirmation message.
        """
        print(f"   [using tool: remember(user={user_id[:8]}…, key={key!r}, value={value!r})]")
        return update_user_memory(user_id, key, value)
    return remember


def create_chat(user_id="anonymous", history=None):
    """Create a fresh Gemini chat, injecting the current user's memory.

    Args:
        user_id: The browser's persistent user ID (used to look up memory).
        history: Optional list of {"role": "user"|"model", "text": ...} dicts to replay.
    """
    remember = _make_remember_tool(user_id)

    chat = get_client().chats.create(
        model=MODEL,
        config=types.GenerateContentConfig(
            system_instruction=build_system_prompt(user_id),
            tools=[
                remember,
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
                    chat.send_message(text)
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

    chat = create_chat(user_id="cli-local")

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
