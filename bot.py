import os
import json
import asyncio
import logging
import subprocess
import httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from fastapi import FastAPI
from fastapi.responses import FileResponse
import uvicorn
from openai import AsyncOpenAI

load_dotenv()

logging.basicConfig(level=logging.INFO)

# --- CONFIGURATION ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")
PUBLIC_URL = os.getenv("PUBLIC_URL", "http://127.0.0.1:8000")
LOG_FILE = "run.jsonl"
LOG_URL = f"{PUBLIC_URL}/run.jsonl"

# 1. Primary AIPIPE Client
aipipe_token = os.getenv("AIPIPE_TOKEN") or os.getenv("AIPROXY_TOKEN")
client_aipipe = AsyncOpenAI(
    api_key=aipipe_token, 
    base_url="https://aipipe.org/openai/v1"
) if aipipe_token else None

# 2. Fallback GROQ Client
groq_key = os.getenv("OPENAI_API_KEY")
client_groq = AsyncOpenAI(
    api_key=groq_key, 
    base_url="https://api.groq.com/openai/v1"
) if groq_key else None

async def call_llm_with_fallback(messages, tools, strict_mode=False):
    errors = []
    
    # Try AIPIPE First
    if client_aipipe:
        try:
            return await client_aipipe.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                tools=tools
            )
        except Exception as e:
            errors.append(f"AIPIPE error: {e}")
            logging.warning(f"AIPIPE failed, falling back to Groq... ({e})")
            
    # Fallback to GROQ
    if client_groq:
        try:
            current_messages = messages
            if strict_mode:
                # Append strict instructions to force Llama 3 to use JSON tools properly
                current_messages = messages + [{"role": "system", "content": "CRITICAL ERROR: You just failed to format a tool call. You MUST use the official JSON schema for tool calls. DO NOT output <function> tags. DO NOT output raw text."}]
                
            return await client_groq.chat.completions.create(
                model="mixtral-8x7b-32768",
                messages=current_messages,
                tools=tools
            )
        except Exception as e:
            # If Groq fails due to tool hallucination, recursively retry once in strict mode
            if not strict_mode and "tool_use_failed" in str(e):
                logging.warning("Groq tool hallucination detected! Retrying with strict instructions...")
                return await call_llm_with_fallback(messages, tools, strict_mode=True)
                
            errors.append(f"GROQ error: {e}")
            
    raise Exception(f"All LLM providers failed. Errors: {errors}")

app = FastAPI()

@app.get("/run.jsonl")
async def serve_log():
    if not os.path.exists(LOG_FILE):
        return FileResponse(LOG_FILE, status_code=404)
    return FileResponse(LOG_FILE)

def append_to_log(user_id, question, answer):
    entry = {
        "user_id": user_id,
        "question": question,
        "answer": answer
    }
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")

tools = [
    {
        "type": "function",
        "function": {
            "name": "python_execute",
            "description": "Execute Python code locally to analyze data and return stdout/stderr. Do not run destructive commands.",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "The Python code to execute"
                    }
                },
                "required": ["code"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Fetch raw text content from a URL. DO NOT use this for large datasets or CSV files! For datasets, use python_execute to download and process them directly in Python.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to fetch"
                    }
                },
                "required": ["url"]
            }
        }
    }
]

async def execute_tool(tool_call):
    name = tool_call.function.name
    args = json.loads(tool_call.function.arguments)
    
    if name == "python_execute":
        code = args.get("code", "")
        try:
            result = subprocess.run(["python3", "-c", code], capture_output=True, text=True, timeout=30)
            return result.stdout if result.returncode == 0 else result.stderr
        except Exception as e:
            return str(e)
            
    elif name == "fetch_url":
        url = args.get("url", "")
        try:
            async with httpx.AsyncClient(verify=False) as client:
                resp = await client.get(url, timeout=30)
                return resp.text[:10000] # Truncate to avoid blowing up context window
        except Exception as e:
            return str(e)
            
    return "Unknown tool"

async def analyze_data_with_llm(context_messages):
    if not client_aipipe and not client_groq:
        return '{"error": "No LLM API keys configured. Set AIPIPE_TOKEN or OPENAI_API_KEY."}'
        
    messages = [{"role": "system", "content": "You are a data-analysis agent. If you need to analyze a dataset, ALWAYS use python_execute to download, read, and process datasets (e.g. using pandas or requests) instead of using the fetch_url tool to save context space. Always return the final answer as a raw JSON object."}] + context_messages
    
    for _ in range(5): # Allow up to 5 tool-calling iterations
        try:
            response = await call_llm_with_fallback(messages, tools)
            msg = response.choices[0].message
            # Groq throws 400 if we pass objects with None values, so we convert it to a clean dictionary
            msg_dict = msg.model_dump(exclude_none=True)
            messages.append(msg_dict)
            
            if msg.tool_calls:
                for tool_call in msg.tool_calls:
                    result = await execute_tool(tool_call)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_call.function.name,
                        "content": str(result)
                    })
            else:
                return msg.content
        except Exception as e:
            return str(e)
            
    return '{"error": "Reached maximum tool iterations"}'

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = update.message.text
    
    # Track conversation context
    if "history" not in context.user_data:
        context.user_data["history"] = []
    
    context.user_data["history"].append({"role": "user", "content": user_text})
    
    # Call LLM
    answer_text = await analyze_data_with_llm(context.user_data["history"])
    
    context.user_data["history"].append({"role": "assistant", "content": answer_text})
    
    # Log the interaction will happen after we construct response_json
    
    # Parse LLM response to avoid double-wrapping and stringified JSON
    clean_text = answer_text.strip()
    if clean_text.startswith("```json"):
        clean_text = clean_text[7:]
    if clean_text.endswith("```"):
        clean_text = clean_text[:-3]
    clean_text = clean_text.strip()
    
    try:
        parsed_answer = json.loads(clean_text)
    except:
        parsed_answer = clean_text

    # Strictly enforce the official document format
    if isinstance(parsed_answer, dict) and "answer" in parsed_answer and "log_url" in parsed_answer:
        response_json = parsed_answer
        response_json["log_url"] = LOG_URL # Ensure it points to our actual host
    else:
        response_json = {
            "answer": parsed_answer,
            "log_url": LOG_URL
        }
    
    # Log the interaction using the final answer sent to the user
    append_to_log(user_id, user_text, response_json)
    
    # Send the response back
    await update.message.reply_text(json.dumps(response_json))

async def run_bot():
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(MessageHandler(filters.TEXT, handle_message))
    
    await application.initialize()
    await application.start()
    await application.updater.start_polling()

async def main():
    # Start FastAPI server in background task
    config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(config)
    asyncio.create_task(server.serve())
    
    # Run Telegram Bot
    await run_bot()
    
    # Keep running
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
