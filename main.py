from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import openmeteo_requests
import pandas as pd
import requests_cache
from retry_requests import retry
from typing import List
import httpx
from pydantic import BaseModel
import asyncio
import json

app = FastAPI()

# Настройка CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

# Настройка Open-Meteo API клиента
cache_session = requests_cache.CachedSession(".cache", expire_after=3600)
retry_session = retry(cache_session, retries=5, backoff_factor=0.2)
openmeteo = openmeteo_requests.Client(session=retry_session)

class HourlyForecast(BaseModel):
    time: str
    temperature: float

class WeatherResponse(BaseModel):
    city: str
    temperature: float
    humidity: float
    wind_speed: float
    description: str
    hourly_forecast: List[HourlyForecast]

async def get_coordinates(city: str) -> tuple[float, float]:
    url = f"https://geocoding-api.open-meteo.com/v1/search?name={city}&count=1"
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        print(f"Geocoding response for {city}: {response.text}")
        if response.status_code != 200:
            raise HTTPException(status_code=500, detail=f"Ошибка при геокодировании: {response.status_code}")
        data = response.json()
        if not data.get("results"):
            raise HTTPException(status_code=404, detail="Город не найден")
        result = data["results"][0]
        return result["latitude"], result["longitude"]

def weather_code_to_description(code: int) -> str:
    codes = {
        0: "Ясно",
        1: "Преимущественно ясно",
        2: "Переменная облачность",
        3: "Пасмурно",
        45: "Туман",
        51: "Легкая морось",
        61: "Небольшой дождь",
        71: "Небольшой снег",
        80: "Ливень",
    }
    return codes.get(code, "Неизвестно")

@app.get("/weather", response_model=WeatherResponse)
async def get_weather(city: str):
    try:
        print(f"Requesting weather for city: {city}")
        lat, lon = await get_coordinates(city)
        print(f"Coordinates: lat={lat}, lon={lon}")
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": lat,
            "longitude": lon,
            "current": ["temperature_2m", "relative_humidity_2m", "wind_speed_10m", "weather_code"],
            "hourly": "temperature_2m"
        }
        responses = openmeteo.weather_api(url, params=params)
        print(f"Weather API response raw: {responses}")
        if not responses:
            raise HTTPException(status_code=500, detail="Нет данных от Open-Meteo API")
        response = responses[0]

        # Получение текущих погодных данных
        current = response.Current()
        current_variables = current.Variables
        current_weather = {
            "temperature": current_variables(0).Value() if current_variables(0) else 0.0,  # temperature_2m
            "humidity": current_variables(1).Value() if current_variables(1) else 0.0,    # relative_humidity_2m
            "wind_speed": current_variables(2).Value() if current_variables(2) else 0.0,  # wind_speed_10m
            "weather_code": current_variables(3).Value() if current_variables(3) else 0   # weather_code
        }

        # Получение почасовых данных
        hourly = response.Hourly()
        hourly_data_var = hourly.Variables(0)
        hourly_temperature_2m = hourly_data_var.ValuesAsNumpy().tolist()

        if not hourly_temperature_2m:
            raise HTTPException(status_code=500, detail="Нет данных о температуре")

        hourly_data = {
            "date": pd.date_range(
                start=pd.to_datetime(hourly.Time(), unit="s", utc=True),
                end=pd.to_datetime(hourly.TimeEnd(), unit="s", utc=True),
                freq=pd.Timedelta(seconds=hourly.Interval()),
                inclusive="left"
            ),
            "temperature_2m": hourly_temperature_2m
        }

        hourly_df = pd.DataFrame(data=hourly_data)

        hourly_forecast = [
            HourlyForecast(
                time=row["date"].strftime("%Y-%m-%d %H:%M"),
                temperature=float(row["temperature_2m"])
            )
            for _, row in hourly_df.head(24).iterrows()
        ]



        return WeatherResponse(
            city=city.capitalize(),
            temperature=current_weather["temperature"],
            humidity=current_weather["humidity"],
            wind_speed=current_weather["wind_speed"],
            description=weather_code_to_description(current_weather["weather_code"]),
            hourly_forecast=hourly_forecast
        )

    except HTTPException as e:
        print(f"HTTP Exception: {e.detail}")
        raise e
    except Exception as exc:
        print(f"Unexpected Exception: {exc}")
        raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера")

app.mount("/", StaticFiles(directory=".", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)