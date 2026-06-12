from jua import JuaClient
from jua.types.geo import LatLon
from jua.weather import Models, Variables

import matplotlib.pyplot as plt

ZURICH = LatLon(lat=47.3769, lon=8.5417)

client = JuaClient()
model = client.weather.get_model(Models.EPT1_5)
forecast = model.get_forecast(
    points=ZURICH,
)[Variables.AIR_TEMPERATURE_AT_HEIGHT_LEVEL_2M]

forecast.to_absolute_time().plot()
plt.title("Temperature in Zurich")
plt.ylabel(Variables.AIR_TEMPERATURE_AT_HEIGHT_LEVEL_2M.display_name_with_unit)
plt.xlabel("Time")
plt.show()