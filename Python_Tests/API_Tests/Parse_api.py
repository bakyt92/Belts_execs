import requests 
import os
import sys

API_KEY = "8ceb63ced657198820d14e20c3c6fb08"

def main(): 
    if len(sys.argv) != 3:
        raise ValueError("There is no sufficient args. It should be 3 args (name of program, lat and lon)")
    lat = float(sys.argv[1])
    lon = float(sys.argv[2])
    response = requests.get("https://api.openweathermap.org/data/2.5/weather",
                            params={"lat": lat, "lon": lon, "appid": API_KEY, "units": "metric"},
                            timeout=10,
    )
    response.raise_for_status()
    data = response.json()
    pressure_cur = data['main']['pressure']
    sea_lvl = data['main']['sea_level']
    print(f"{data}, \npressure on spot is {pressure_cur} and sea level of spot is {sea_lvl}")
    return
    
if __name__ == "__main__":
    main()