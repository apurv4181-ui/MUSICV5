# Copyright (c) 2025 AnonymousX1025
# Licensed under the MIT License.

from html import escape
from io import BytesIO
import json
import aiohttp
import urllib.parse
import re
from PIL import Image
from pyrogram import filters, types
from anony import app

# Image generation API
_IMAGE_API = "https://v1-img-gen.prathmeshapis.workers.dev/generate"
_MAX_RESPONSE_BYTES = 15 * 1024 * 1024

# Number info API
_NUM_API_URL = "https://apurv-num-info-api.prathmeshapis.workers.dev/"

# AI API providers
_CLAUDE_API_URL = "https://kilwaapi.vercel.app/kilwa-claude"
_GROK_API_URL = "https://kilwaapi.vercel.app/kilwa-grok"

# Per-user AI provider preference (default: claude)
user_ai_provider = {}


async def _fetch_generated_image(prompt: str) -> BytesIO:
    """Fetch an image from the generator and return a Telegram-ready PNG."""
    timeout = aiohttp.ClientTimeout(total=90)
    headers = {"User-Agent": "Mozilla/5.0"}

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(_IMAGE_API, params={"prompt": prompt}) as response:
            if response.status != 200:
                raise RuntimeError(f"image API returned HTTP {response.status}")

            content_length = response.content_length
            if content_length and content_length > _MAX_RESPONSE_BYTES:
                raise RuntimeError("image response is too large")

            image_data = await response.read()

    if not image_data or len(image_data) > _MAX_RESPONSE_BYTES:
        raise RuntimeError("image response is empty or too large")

    with Image.open(BytesIO(image_data)) as image:
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGBA")

        output = BytesIO()
        image.save(output, format="PNG", optimize=True)
        output.seek(0)
        output.name = "generated.png"
        return output


async def _query_ai(api_url: str, query: str) -> str | None:
    """Query an AI API endpoint and return the reply text."""
    encoded_query = urllib.parse.quote(query)
    full_url = f"{api_url}?text={encoded_query}"

    timeout = aiohttp.ClientTimeout(total=60)
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(full_url) as response:
                if response.status != 200:
                    return None

                response_text = await response.text()

        try:
            data = json.loads(response_text)

            if data.get("status") != "success":
                return None

            reply = data.get("reply", "")

            if reply:
                return reply.strip()

        except json.JSONDecodeError:
            if '"reply"' in response_text:
                try:
                    start_idx = response_text.find('"reply":') + 8
                    while start_idx < len(response_text) and response_text[start_idx] in ' \t\n"':
                        start_idx += 1
                    end_idx = response_text.find('"', start_idx)
                    if end_idx > start_idx:
                        return response_text[start_idx:end_idx].strip()
                except Exception:
                    pass

        return None

    except Exception:
        return None


def _format_stylish_response(text: str) -> str:
    """
    Format the AI response in a stylish, clean way for Telegram HTML.

    - Converts markdown code blocks to <pre><code> blocks
    - Converts inline `code` to <code>
    - Converts **bold** to <b>
    - Converts *italic* / _italic_ to <i>
    - Removes markdown headers (###) and turns them into bold lines
    - Fixes bullet points and numbered lists
    - Escapes HTML safely
    """
    if not text:
        return ""

    # First, escape HTML so user content can't break formatting
    text = escape(text)

    # ---- Code blocks: ```lang\n...\n``` -> <pre><code>...</code></pre> ----
    def _code_block(match):
        code = match.group(2)
        # Remove leading newline inside the code block
        if code.startswith("\n"):
            code = code[1:]
        code = code.rstrip("\n")
        return f"<pre><code>{code}</code></pre>"

    text = re.sub(r"```([a-zA-Z0-9_+-]*)\n(.*?)```", _code_block, text, flags=re.DOTALL)

    # ---- Inline code: `code` -> <code>code</code> ----
    text = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", text)

    # ---- Headers: ### Header / ## Header / # Header -> bold line ----
    text = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)

    # ---- Bold: **text** -> <b>text</b> ----
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)

    # ---- Italic: *text* -> <i>text</i> ----
    text = re.sub(r"(?<![\*\w])\*([^\*\n]+?)\*(?![\*\w])", r"<i>\1</i>", text)

    # ---- Italic: _text_ -> <i>text</i> ----
    text = re.sub(r"(?<![_\w])_([^_\n]+?)_(?![_\w])", r"<i>\1</i>", text)

    # ---- Strikethrough: ~~text~~ -> <s>text</s> ----
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text, flags=re.DOTALL)

    # ---- Bullet points: "- " / "* " at line start to nicer bullet ----
    text = re.sub(r"^[\-\*]\s+", "• ", text, flags=re.MULTILINE)

    # ---- Collapse 3+ blank lines into 2 ----
    text = re.sub(r"\n{3,}", "\n\n", text)

    # ---- Trim leading/trailing whitespace ----
    text = text.strip()

    return text


def _split_message(text: str, limit: int = 4000) -> list[str]:
    """Split a long message into chunks that respect paragraph breaks."""
    if len(text) <= limit:
        return [text]

    chunks = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n\n", 0, limit)
        if split_at == -1 or split_at < limit // 2:
            split_at = remaining.rfind("\n", 0, limit)
        if split_at == -1 or split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


async def _send_stylish(m: types.Message, reply: str, source_name: str = None) -> None:
    """Send the AI reply in a clean, stylish format (split if needed)."""
    formatted = _format_stylish_response(reply)

    header = "🤖 <b>AI Response</b>"
    if source_name:
        header = f"🤖 <b>{source_name}</b>"

    full_text = f"{header}\n━━━━━━━━━━━━━━━━━━━━\n\n{formatted}"

    chunks = _split_message(full_text, limit=4000)

    for i, chunk in enumerate(chunks):
        if i == 0:
            await m.reply_text(chunk, quote=True, disable_web_page_preview=True)
        else:
            await m.reply_text(chunk, quote=False, disable_web_page_preview=True)


async def _handle_ai_query(m: types.Message, query: str, provider: str) -> None:
    """Handle AI query with fallback to the other provider."""
    status = await m.reply_text(
        "🤖 <b>Thinking...</b>",
        quote=True,
    )

    if provider == "claude":
        primary_url = _CLAUDE_API_URL
        fallback_url = _GROK_API_URL
        primary_name = "Claude"
        fallback_name = "Grok"
    else:
        primary_url = _GROK_API_URL
        fallback_url = _CLAUDE_API_URL
        primary_name = "Grok"
        fallback_name = "Claude"

    reply = await _query_ai(primary_url, query)
    used_provider = primary_name

    if not reply:
        try:
            await status.edit_text(
                f"⚠️ <b>{primary_name} failed.</b>\n"
                f"<i>Trying {fallback_name}...</i>"
            )
        except Exception:
            pass
        reply = await _query_ai(fallback_url, query)
        used_provider = fallback_name

    if not reply:
        await status.edit_text(
            "❌ <b>Both providers failed.</b>\n"
            "Please try again in a moment."
        )
        return

    # Delete "Thinking..." status, then send formatted response
    try:
        await status.delete()
    except Exception:
        pass

    await _send_stylish(m, reply, source_name=used_provider)


@app.on_message(filters.command("img") & ~app.bl_users)
async def image_generation(_, m: types.Message) -> None:
    """Generate an image from a text prompt."""
    if len(m.command) < 2:
        await m.reply_text(
            "🎨 <b>Usage:</b> <code>/img your image prompt</code>",
            quote=True,
        )
        return

    prompt = " ".join(m.command[1:]).strip()
    status = await m.reply_text(
        "🎨 <b>Generating your image...</b>",
        quote=True,
    )

    try:
        image = await _fetch_generated_image(prompt)
        caption = f"🎨 <b>Prompt:</b> {escape(prompt)}"
        await m.reply_photo(photo=image, caption=caption, quote=True)
        await status.delete()
    except Exception as e:
        try:
            await status.edit_text(
                f"❌ <b>Image generation failed.</b>\n"
                f"Error: {str(e)[:100]}\n"
                "Please try a different prompt in a moment.",
            )
        except Exception:
            pass


@app.on_message(filters.command("num") & ~app.bl_users)
async def number_search(_, m: types.Message) -> None:
    """Search for mobile number information."""
    if len(m.command) < 2:
        await m.reply_text(
            "🔍 <b>Usage:</b> <code>/num mobile_number</code>\n"
            "Example: <code>/num 9876543210</code>",
            quote=True,
        )
        return

    mobile_number = m.command[1].strip()

    if not mobile_number.isdigit() or len(mobile_number) < 10:
        await m.reply_text(
            "❌ <b>Invalid mobile number.</b>\n"
            "Please provide a valid 10-digit number.",
            quote=True,
        )
        return

    status = await m.reply_text(
        f"🔍 <b>Searching for {mobile_number}...</b>",
        quote=True,
    )

    try:
        full_url = f"{_NUM_API_URL}?mobile={mobile_number}"

        async with aiohttp.ClientSession() as session:
            async with session.get(full_url, timeout=30) as response:
                if response.status != 200:
                    await status.edit_text(
                        f"❌ <b>API Error.</b>\n"
                        f"Status: {response.status}"
                    )
                    return

                response_text = await response.text()

        try:
            data = json.loads(response_text)
        except json.JSONDecodeError:
            await status.edit_text("❌ <b>Invalid response from API.</b>")
            return

        result = data.get("result")

        if result is None:
            await status.edit_text(f"❌ <b>No result found for {mobile_number}</b>")
            return

        found = result.get("found", 0)
        data_list = result.get("data", [])

        if found == 0 or not data_list:
            await status.edit_text(f"📱 <b>No information found for {mobile_number}</b>")
            return

        formatted_result = format_number_result(result, mobile_number)
        await status.edit_text(formatted_result)

    except Exception as e:
        await status.edit_text(
            f"❌ <b>An error occurred.</b>\n"
            f"Error: {str(e)[:100]}"
        )


@app.on_message(filters.command("ai") & ~app.bl_users)
async def claude_ai(_, m: types.Message) -> None:
    """Chat with Claude AI (with Grok fallback)."""
    chat_id = m.chat.id

    if len(m.command) < 2:
        await m.reply_text(
            "🤖 <b>Claude AI</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "<b>Usage:</b> <code>/ai your question here</code>\n"
            "<b>Example:</b> <code>/ai What is the meaning of life?</code>\n\n"
            "💡 <i>Switches to Claude and answers.</i>",
            quote=True,
        )
        return

    user_ai_provider[chat_id] = "claude"
    query = " ".join(m.command[1:]).strip()
    await _handle_ai_query(m, query, provider="claude")


@app.on_message(filters.command("grok") & ~app.bl_users)
async def grok_ai(_, m: types.Message) -> None:
    """Chat with Grok AI (with Claude fallback)."""
    chat_id = m.chat.id

    if len(m.command) < 2:
        await m.reply_text(
            "🌌 <b>Grok AI</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "<b>Usage:</b> <code>/grok your question here</code>\n"
            "<b>Example:</b> <code>/grok Tell me a joke</code>\n\n"
            "💡 <i>Switches to Grok and answers.</i>",
            quote=True,
        )
        return

    user_ai_provider[chat_id] = "grok"
    query = " ".join(m.command[1:]).strip()
    await _handle_ai_query(m, query, provider="grok")


@app.on_message(filters.command("switch") & ~app.bl_users)
async def switch_provider(_, m: types.Message) -> None:
    """Show or switch the current AI provider."""
    chat_id = m.chat.id
    current = user_ai_provider.get(chat_id, "claude")

    if len(m.command) > 1:
        choice = m.command[1].strip().lower()
        if choice in ("claude", "ai"):
            user_ai_provider[chat_id] = "claude"
            await m.reply_text(
                "✅ <b>Switched to Claude</b>\n"
                "Use <code>/ai &lt;question&gt;</code> to chat.",
                quote=True,
            )
        elif choice in ("grok", "g"):
            user_ai_provider[chat_id] = "grok"
            await m.reply_text(
                "✅ <b>Switched to Grok</b>\n"
                "Use <code>/grok &lt;question&gt;</code> to chat.",
                quote=True,
            )
        else:
            await m.reply_text(
                "❌ Unknown provider. Use <code>/switch claude</code> or <code>/switch grok</code>.",
                quote=True,
            )
        return

    await m.reply_text(
        f"🤖 <b>Current provider:</b> <code>{current}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>Switch with:</b>\n"
        "▪ <code>/switch claude</code>\n"
        "▪ <code>/switch grok</code>\n\n"
        "<b>Or use directly:</b>\n"
        "▪ <code>/ai &lt;question&gt;</code>\n"
        "▪ <code>/grok &lt;question&gt;</code>",
        quote=True,
    )


def format_number_result(result, mobile_number):
    """Format the result data for display."""
    found = result.get("found", 0)
    data_list = result.get("data", [])

    if found == 0 or not data_list:
        return f"📱 <b>No information found for {mobile_number}</b>"

    first_entry = data_list[0]

    response = f"📱 <b>Mobile Number:</b> <code>{mobile_number}</code>\n"
    response += f"📊 <b>Records Found:</b> {found}\n\n"

    if first_entry.get("name"):
        response += f"👤 <b>Name:</b> {escape(first_entry.get('name'))}\n"

    if first_entry.get("fname"):
        response += f"👨 <b>Father's Name:</b> {escape(first_entry.get('fname'))}\n"

    if first_entry.get("address"):
        response += f"📍 <b>Address:</b> {escape(first_entry.get('address'))}\n"

    if first_entry.get("email"):
        response += f"📧 <b>Email:</b> {escape(first_entry.get('email'))}\n"

    if first_entry.get("id"):
        response += f"🆔 <b>ID:</b> {escape(first_entry.get('id'))}\n"

    if found > 1:
        response += f"\n⚠️ <b>Note:</b> {found} entries found for this number."

    return response
