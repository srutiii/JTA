# Google Gemini API Setup

## Installation

1. Install the required package:
```bash
pip install google-generativeai
```

2. Get your API key from Google AI Studio:
   - Visit: https://makersuite.google.com/app/apikey
   - Create a new API key
   - Copy the API key

## Configuration

### Option 1: Environment Variable (Recommended)

Set the environment variable before running the app:

**Windows (PowerShell):**
```powershell
$env:GEMINI_API_KEY="your-api-key-here"
python app.py
```

**Windows (Command Prompt):**
```cmd
set GEMINI_API_KEY=your-api-key-here
python app.py
```

**Linux/Mac:**
```bash
export GEMINI_API_KEY="your-api-key-here"
python app.py
```

### Option 2: .env File (Alternative)

Create a `.env` file in the project root:
```
GEMINI_API_KEY=your-api-key-here
```

Then install python-dotenv and load it in app.py:
```python
from dotenv import load_dotenv
load_dotenv()
```

## Features Enabled

Once configured, the following AI-powered features are available:

1. **CV Text Extraction** - Intelligent extraction of structured data from CVs
2. **Auto-Fill Profile** - Automatically populate "About Me" fields from CV
3. **Smart Data Normalization** - Clean, recruiter-friendly formatting

## API Usage

The system uses Google's Gemini Pro model with:
- Low temperature (0.1) for deterministic extraction
- Strict JSON output format
- No hallucination - only extracts what's actually in the CV

## Cost

Google Gemini API has a free tier. Check current pricing at:
https://ai.google.dev/pricing

