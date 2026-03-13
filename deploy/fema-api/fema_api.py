#!/usr/bin/env python3
"""
FEMA Flood Map API
FastAPI server for FEMA flood data extraction.

Run:
  pip install fastapi uvicorn requests folium geopy playwright
  playwright install chromium
  uvicorn main:app --reload

API Docs: http://localhost:8000/docs
"""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional
import requests
import json
import folium
from folium.plugins import GeoJsonTooltip
import base64
import io
from playwright.async_api import async_playwright
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter
import asyncio
import os

app = FastAPI(title="FEMA Flood Map API", version="1.0")

class ExtractRequest(BaseModel):
    address: Optional[str] = None
    fips: Optional[str] = None
    state: Optional[str] = None
    bbox: Optional[str] = None  # "minlon,minlat,maxlon,maxlat"

async def geocode_address(address):
    geolocator = Nominatim(user_agent="fema_api")
    geocode = RateLimiter(geolocator.geocode, min_delay_seconds=1)
    location = await asyncio.to_thread(geocode, address)
    if not location:
        raise HTTPException(404, "Address not found")
    bbox = location.raw['boundingbox']
    return f"{bbox[2]},{bbox[0]},{bbox[3]},{bbox[1]}"  # minlon,minlat,maxlon,maxlat

def fetch_flood_zones(fips=None, bbox=None, state=None):
    base_url = "https://api.fema.gov/open/v2/FIRMZone"
    params = {"format": "geojson", "$limit": 1000}
    if fips:
        params["county"] = fips
    if bbox:
        params["polygon"] = bbox
    if state:
        params["state"] = state
    response = requests.get(base_url, params=params)
    response.raise_for_status()
    return response.json()

def download_firm_pdf(fips, state):
    pdf_url = f"https://hazards.fema.gov/femaportal/wps/wcm/connect/3d4c4b00-1b0a-4b0a-9b0a-4b0a4b0a4b0a/{state.upper()}-{fips}-FIRM.pdf"
    response = requests.get(pdf_url)
    if response.status_code == 200:
        return base64.b64encode(response.content).decode()
    return None

async def generate_map_screenshot(geojson_data):
    bounds = geojson_data['features'][0]['geometry']['coordinates'][0][0] if geojson_data['features'] else [[29.7, -95.4]]
    m = folium.Map(location=bounds[0], zoom_start=12)
    
    folium.GeoJson(
        geojson_data,
        style_function=lambda x: {'fillColor': 'blue', 'color': 'black', 'weight': 1, 'fillOpacity': 0.5},
        tooltip=GeoJsonTooltip(fields=['FLD_ZONE', 'SFHA'])
    ).add_to(m)
    
    html_path = "temp_map.html"
    m.save(html_path)
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_viewport_size({"width": 1200, "height": 800})
        await page.goto(f"file://{os.path.abspath(html_path)}")
        screenshot = await page.screenshot(full_page=False)
        await browser.close()
    
    os.remove(html_path)
    return base64.b64encode(screenshot).decode()

@app.post("/extract")
async def extract_flood_data(request: ExtractRequest):
    bbox = None
    fips_state = None
    
    if request.address:
        bbox = await geocode_address(request.address)
    elif request.fips or request.state:
        fips_state = (request.fips, request.state)
    
    data = fetch_flood_zones(fips=request.fips, bbox=bbox, state=request.state)
    
    if not data['FIRMZone']:
        raise HTTPException(404, "No flood data found for location")
    
    result = {
        "zones": len(data['FIRMZone']),
        "sample_zone": data['FIRMZone'][0],
        "geojson": data,
        "map_png_b64": await generate_map_screenshot(data),
        "pdf_b64": None
    }
    
    if fips_state:
        pdf_b64 = download_firm_pdf(*fips_state)
        if pdf_b64:
            result["pdf_b64"] = pdf_b64
    
    return result

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
