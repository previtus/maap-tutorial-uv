from datetime import datetime
import sys
import os
import os.path
from timeit import default_timer as timer
import argparse
from typing import Tuple, Optional, Any, Union
from datetime import datetime

print("About to import rio")

import rasterio as rio
from rasterio.warp import calculate_default_transform
import rasterio
import rasterio.windows
import rasterio.warp


print("About to import georeader")
from georeader.readers import emit
from georeader.save import save_cog
from georeader.rasterio_reader import RasterioReader
from georeader.geotensor import GeoTensor
from georeader import rasterize, read

print("About to import netCDF4")
import netCDF4

print("About to import earthaccess")
import earthaccess

print("About to import geopandas")
import geopandas as gpd

print("About to import geojson")
import geojson

print("About to import json, shapely")
import json
from shapely.geometry import Polygon, mapping

print("About to import torch")
import torch

print("About to import segmentation_models_pytorch")
import segmentation_models_pytorch as smp


print("About to import 'from osgeo import gdal'")
from osgeo import gdal

"""
fails with:
    from osgeo import gdal
ModuleNotFoundError: No module named 'osgeo'
(so gdal is not installed propely?)
"""

print("Passed all tested imports!")
