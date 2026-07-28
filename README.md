# Telegram Data-Analysis Agent

This directory contains a Telegram bot that acts as a multi-turn LLM data-analysis agent, along with a lightweight FastAPI server that simultaneously exposes the agent's run log (`run.jsonl`) to the public internet.

## Local Setup

1. A Python virtual environment has been created in `venv`.
2. Activate it using: `source venv/bin/activate`
3. The dependencies are saved in `requirements.txt`. Install them using: `pip install -r requirements.txt`
4. Create a `.env` file in the `Qn4` directory and set your environment variables:
   - `BOT_TOKEN`: Your Telegram Bot token from @BotFather.
   - `OPENAI_API_KEY` (or `AIPROXY_TOKEN`): Your API key for data analysis.
   - `PUBLIC_URL`: The public IP or domain where the log is accessible (e.g., `http://1.2.3.4:8000`).
5. Run the bot: `python3 bot.py`

## GCP Deployment Instructions (Strict Free Tier)

To deploy this application to Google Cloud Platform while strictly remaining within the **Always Free Tier** limits, follow these execution steps.

### 1. Create the VM Instance
Run this command from your local machine (where `gcloud` is authenticated) or inside Cloud Shell. It guarantees you use an `e2-micro`, in `us-central1`, with a `30GB` Standard Persistent Disk.

```bash
gcloud compute instances create telegram-agent-vm \
    --project=YOUR_PROJECT_ID \
    --zone=us-central1-a \
    --machine-type=e2-micro \
    --network-interface=network-tier=PREMIUM,subnet=default \
    --create-disk=auto-delete=yes,boot=yes,device-name=telegram-agent-vm,image=projects/debian-cloud/global/images/family/debian-12,mode=rw,size=30,type=pd-standard \
    --tags=http-server
```

### 2. Open Firewall Port for the Web Server
Your FastAPI server will run on port 8000 to serve `run.jsonl`. Open this port:

```bash
gcloud compute firewall-rules create allow-log-server \
    --action=allow \
    --direction=ingress \
    --rules=tcp:8000 \
    --source-ranges=0.0.0.0/0 \
    --target-tags=http-server
```

### 3. Connect to the VM & Environment Setup
SSH into the VM:
```bash
gcloud compute ssh telegram-agent-vm --zone=us-central1-a
```

Once inside the VM, set up your Python environment:
```bash
sudo apt update && sudo apt install -y python3-venv python3-pip git tmux
git clone <YOUR_REPOSITORY_URL> # or use scp to copy the Qn4 folder
cd Qn4
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 4. Continuous Execution (using tmux)
To ensure the bot and server run continuously after you close the SSH session, use `tmux`:

```bash
tmux new -s bot_session
source venv/bin/activate

# Setup your .env file
nano .env # Or copy it over

# Ensure your .env file contains:
# BOT_TOKEN="your_bot_token"
# OPENAI_API_KEY="your_api_key_or_aiproxy_token"
# PUBLIC_URL="http://$(curl -s ifconfig.me):8000"

# Run the agent
python3 bot.py
```

*Press `Ctrl+B` then `D` to detach from the tmux session.* Your bot is now live! The log will be publicly accessible at `http://<VM_EXTERNAL_IP>:8000/run.jsonl`.
