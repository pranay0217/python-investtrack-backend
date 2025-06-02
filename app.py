from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
import os
from dotenv import load_dotenv
from pymongo import MongoClient
from fastapi.middleware.cors import CORSMiddleware
import google.generativeai as genai
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
import json
from datetime import datetime
import http.client

# Load environment variables
load_dotenv()

app = FastAPI()

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://investtrack-4xgu.onrender.com"],  # Update this as needed
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# MongoDB connection
client = MongoClient(os.getenv("MONGODB_URI"))
db = client["INVESTTRACK"]
angelone_collection = db["Holdings"]
zerodha_collection = db["holdings"]

# Initialize Google Gemini LLM
genai.configure(api_key=os.getenv("GOOGLE_API_KEY"))
llm1 = genai.GenerativeModel("gemini-2.0-flash")

class AuthData(BaseModel):
    username: str
    clientcode: str
    token: str

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={"detail": exc.errors(), "body": await request.json()},
    )

@app.post("/fetch_portfolio")
def fetch_and_store_holdings(data: AuthData):
    username = data.username
    clientcode = data.clientcode
    token = data.token
    now = datetime.now()

    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'X-UserType': 'USER',
        'X-SourceID': 'WEB',
        'X-ClientLocalIP': os.getenv("CLIENT_LOCAL_IP", "127.0.0.1"),
        'X-ClientPublicIP': os.getenv("CLIENT_PUBLIC_IP", "127.0.0.1"),
        'X-MACAddress': os.getenv("MAC_ADDRESS", "44:38:39:ff:ef:57"),
        'X-PrivateKey': os.getenv("ANGEL_API_KEY")
    }

    try:
        conn = http.client.HTTPSConnection("apiconnect.angelone.in")
        conn.request("GET", "/rest/secure/angelbroking/portfolio/v1/getAllHolding", "", headers)
        res = conn.getresponse()
        response_data = res.read().decode("utf-8")

        if res.status != 200:
            raise HTTPException(status_code=res.status, detail="Failed to fetch holdings")

        parsed_data = json.loads(response_data)
        new_holdings = parsed_data.get("data", [])

        # Update AngelOne holdings in MongoDB
        angelone_collection.update_one(
            {"username": username, "broker": "angelone"},
            {
                "$set": {
                    "holdings": new_holdings,
                    "last_updated": now,
                    "username": username,
                    "broker": "angelone"
                }
            },
            upsert=True
        )

        # TODO: Add fetching Zerodha holdings here if you want (similar pattern)

        return {
            "success": True,
            "message": "Holdings updated successfully",
            "data": new_holdings
        }

    except Exception as e:
        print(f"Exception: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error")


@app.get("/analyze")
def analyze_holdings(username: str):
    try:
        # Fetch holdings from both brokers (if exist)
        record_angelone = angelone_collection.find_one({"username": username, "broker": "angelone"})
        record_zerodha = zerodha_collection.find_one({"username": username})

        holdings_angelone = []
        holdings_zerodha = []

        if record_angelone and "holdings" in record_angelone:
            # AngelOne's holdings are nested in "holdings" key inside holdings
            holdings_angelone = record_angelone["holdings"].get("holdings", []) if isinstance(record_angelone["holdings"], dict) else record_angelone["holdings"]

        if record_zerodha and "holdings" in record_zerodha:
            holdings_zerodha = record_zerodha["holdings"]

        if not holdings_angelone and not holdings_zerodha:
            return {
                "success": False,
                "message": "No holdings found for the user.",
                "data": None
            }

        combined_holdings = []
        if holdings_angelone:
            combined_holdings.append({"broker": "angelone", "holdings": holdings_angelone})
        if holdings_zerodha:
            combined_holdings.append({"broker": "zerodha", "holdings": holdings_zerodha})

        # Flatten holdings for LLM input
        flattened_holdings = []
        for broker_data in combined_holdings:
            broker = broker_data["broker"]
            holdings_list = broker_data["holdings"]

            if isinstance(holdings_list, list):
                for h in holdings_list:
                    investment_value = float(h.get("investment_value", 0)) if 'investment_value' in h else 0
                    current_value = float(h.get("current_value", 0)) if 'current_value' in h else 0
                    profit_loss = current_value - investment_value

                    holding = {
                        "broker": broker,
                        "name": h.get("tradingsymbol", "Unknown"),
                        "quantity": float(h.get("quantity", 0)),
                        "avg_price": float(h.get("averageprice", h.get("average_price", 0))),
                        "profit_loss": profit_loss,
                    }
                    flattened_holdings.append(holding)

        # Prepare LLM prompt with flattened holdings
        analysis_prompt = f"""
You are a financial advisor AI in an investment app.
Based on the user's current holdings, compare each fund with a better alternative if available.
Use the following format:

Trust Score: Display fund manager experience as stars, e.g., ⭐⭐⭐ for moderate, ⭐⭐⭐⭐⭐ for excellent.

Risk: Show as a “Safety Meter” — 🟢 Low Risk, 🟡 Medium, 🔴 High.

Returns: Show as ₹X → ₹Y in Z time with a 📈 emoji.

Comparison Statement: "Fund A is better than Fund B: 15% returns, same risk. Switch now? ✅"

Use these parameters as input:

holdings = {json.dumps(flattened_holdings, indent=2)}

Your task is to analyze if a better-performing fund (with equal or lower risk and equal or higher trust score) exists for each holding.
Present the comparison in the format above.
Just return the answer only on the given format nothing else should be concluded in the answer.
        """

        # Call the LLM for analysis
        response = llm1.generate_content(analysis_prompt)

        return {
            "success": True,
            "message": "AI financial advisory completed successfully",
            "data": {
                "combined_holdings": combined_holdings,
                "advisory": response.text
            }
        }

    except Exception as e:
        print(f"Error during LLM completion: {e}")
        raise HTTPException(status_code=500, detail="AI analysis failed")
