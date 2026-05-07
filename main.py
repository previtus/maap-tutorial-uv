"""Create a cloud-free composite image from a temporal mosaic of HLS granules"""

import argparse
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from functools import partial
from typing import Tuple

import odc.stac
import rasterio
import rioxarray  # noqa
from maap.maap import MAAP
from odc.geo.geobox import GeoBox
from odc.stac import ParsedItem
from pyproj import CRS
from pystac import Asset, Catalog, CatalogType, Item, MediaType
from rasterio.session import AWSSession
from rasterio.warp import transform_bounds
from rio_stac import create_stac_item
from rustac import DuckdbClient

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
logging.getLogger("botocore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

BBox = Tuple[float, float, float, float]

MEMORY_GB = 8
GDAL_CONFIG = {
    "CPL_TMPDIR": "/tmp",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": "TIF",
    "GDAL_CACHEMAX": "75%",
    "GDAL_INGESTED_BYTES_AT_OPEN": "32768",
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "GDAL_HTTP_MULTIPLEX": "YES",
    "GDAL_HTTP_VERSION": "2",
    "PYTHONWARNINGS": "ignore",
    "VSI_CACHE": "TRUE",
    "VSI_CACHE_SIZE": "536870912",
    "GDAL_NUM_THREADS": "ALL_CPUS",
    # "CPL_DEBUG": "ON" if debug else "OFF",
    # "CPL_CURL_VERBOSE": "YES" if debug else "NO",
}

HLS_COLLECTIONS = ["HLSL30_2.0", "HLSS30_2.0"]
HLS_STAC_GEOPARQUET_HREF = "s3://nasa-maap-data-store/file-staging/nasa-map/hls-stac-geoparquet-archive/v2/{collection}/**/*.parquet"

URL_PREFIX = "https://data.lpdaac.earthdatacloud.nasa.gov/"
DTYPE = "int16"
FMASK_DTYPE = "uint8"
NODATA = -9999
FMASK_NODATA = 255
HLS_ODC_STAC_CONFIG = {
    "HLSL30_2.0": {
        "assets": {
            "*": {
                "nodata": NODATA,
                "data_type": DTYPE,
            },
            "Fmask": {
                "nodata": FMASK_NODATA,
                "data_type": FMASK_DTYPE,
            },
        },
        "aliases": {
            "coastal_aerosol": "B01",
            "blue": "B02",
            "green": "B03",
            "red": "B04",
            "nir": "B05",
            "swir_1": "B06",
            "swir_2": "B07",
            "cirrus": "B09",
            "thermal_infrared_1": "B10",
            "thermal": "B11",
        },
    },
    "HLSS30_2.0": {
        "assets": {
            "*": {
                "nodata": NODATA,
                "data_type": DTYPE,
            },
            "Fmask": {
                "nodata": FMASK_NODATA,
                "data_type": FMASK_DTYPE,
            },
        },
        "aliases": {
            "coastal_aerosol": "B01",
            "blue": "B02",
            "green": "B03",
            "red": "B04",
            "red_edge_1": "B05",
            "red_edge_2": "B06",
            "red_edge_3": "B07",
            "nir_broad": "B08",
            "nir": "B8A",
            "water_vapor": "B09",
            "cirrus": "B10",
            "swir_1": "B11",
            "swir_2": "B12",
        },
    },
}

# these are the ones that we are going to use
DEFAULT_BANDS = ["red", "green", "blue", "nir", "swir_1", "swir_2"]
DEFAULT_RESOLUTION = 30
INDEX_FUNS = {
    'ndvi': lambda s: (s.nir - s.red) / (s.nir + s.red),
    'savi': lambda s: (s.nir - s.red) / (s.nir + s.red + 0.5) * 1.5,
    'msavi':lambda s: (2 * s.nir + 1 - ((2 * s.nir + 1)**2 - 8 * (s.nir - s.red))**0.5) / 2,

    'evi': lambda s: 2.5 * (s.nir - s.red) / (s.nir + 6 * s.red - 7.5 * s.blue + 1),
    'evi2': lambda s: 2.5 * (s.nir - s.red) / (s.nir + 2.4 * s.red + 1),

    'ndmi':lambda s: (s.nir - s.swir_1) / (s.nir + s.swir_1),
    'ndsi':lambda s: (s.green - s.swir_1) / (s.green + s.swir_1),

    'nbr': lambda s: (s.nir - s.swir_2) / (s.nir + s.swir_2),
    'nbr2': lambda s: (s.swir_1 - s.swir_2) / (s.swir_1 + s.swir_2),
}

def mask_and_scale(stack, bands):
    """
    Apply cloud, high aerosol, and range mask to stack and scale
    """
    cloud_bitmask = 14
    high_aero_bitmask = 0b11000000
    scale = 0.0001

    mask = (stack.Fmask & cloud_bitmask) == 0
    aero_mask = (stack.Fmask & high_aero_bitmask) != high_aero_bitmask
    range_mask = (stack[bands] != NODATA) & (stack[bands] > 0) & (stack[bands] < 10000)
    mask = mask & aero_mask & range_mask
    cloud_free = stack[bands].where(mask).where(stack[bands] != NODATA) * scale
    cloud_free['Fmask'] = stack.Fmask.where(stack.Fmask != FMASK_NODATA, NODATA).astype('float32')
    cloud_free['Fmask'].attrs.pop('nodata', None)

    return cloud_free

def max_ndvi_composite(stack):
    ndvi = (stack.nir - stack.red) / (stack.nir + stack.red)
    valid_ndvi = ndvi.notnull().any(dim='time')
    idx = ndvi.fillna(NODATA).argmax(dim='time').compute()
    comp = stack.isel(time=idx).where(valid_ndvi).fillna(NODATA).compute()
    return comp

def quantile_ndvi_composite(stack, q):
    ndvi = (stack.nir - stack.red) / (stack.nir + stack.red)
    quant = ndvi.quantile(q, dim='time', skipna=True)
    idx = abs(ndvi - quant).fillna(1e6).argmin(dim='time').compute()
    comp = stack.isel(time=idx).fillna(NODATA).compute()
    return comp, idx

def median_composite(stack):
    return stack.median(dim="time", skipna=True).fillna(NODATA).compute()

def calculate_composite(stack, indices_to_add, method, q):
    if method in ('qndvi', 'maxndvi'):
        q = 1 if method == 'maxndvi' else q
        comp, idx = quantile_ndvi_composite(stack, q)
        comp['doy'] = stack.time.isel(time=idx).dt.dayofyear.astype('float32')
        comp['Fmask'] = stack.Fmask.isel(time=idx)
        for k in indices_to_add:
            comp[k] = INDEX_FUNS[k](stack).isel(time=idx)

    elif method == 'median':
         # median Fmask doesn't make sense
        stack = stack.drop_vars('Fmask')
        comp = stack.median(dim="time", skipna=True)
        for k in indices_to_add:
            comp[k] = INDEX_FUNS[k](stack).median(dim="time", skipna=True)

    return comp.fillna(NODATA).compute()


DUCKDB_EXTENSION_DIRECTORY = Path(os.environ["HOME"]) / "duckdb-extensions"

if not DUCKDB_EXTENSION_DIRECTORY.exists():
    raise FileNotFoundError(f"{DUCKDB_EXTENSION_DIRECTORY} does not exist")


def parse_datetime_utc(dt_string: str) -> datetime:
    """
    Parse a datetime string and ensure it has UTC timezone.
    If no timezone is specified, assume UTC.

    Args:
        dt_string: ISO format datetime string (e.g., '2024-01-01T00:00:00' or '2024-01-01T00:00:00Z')

    Returns:
        datetime object with UTC timezone
    """
    dt = datetime.fromisoformat(dt_string.replace("Z", "+00:00"))

    # If the datetime is naive (no timezone), assume UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt


def validate_crs_units_in_meters(crs: CRS) -> None:
    """
    Validate that the CRS uses meters as its linear unit.

    Args:
        crs: The CRS to validate

    Raises:
        ValueError: If the CRS does not use meters as its linear unit
    """
    # Get the axis info to check units
    axis_info = crs.axis_info

    if not axis_info:
        raise ValueError(
            f"Cannot determine units for CRS '{crs}'. "
            "Please provide a CRS with meter units."
        )

    # Check if any axis uses non-meter units
    for axis in axis_info:
        unit_name = axis.unit_name.lower()
        # Common meter unit names: "metre", "meter", "m"
        if unit_name not in ["metre", "meter", "m"]:
            raise ValueError(
                f"CRS '{crs}' uses '{axis.unit_name}' units, but only CRS with meter units are supported. "
                f"Please provide a CRS that uses meters (e.g., UTM zones, Web Mercator)."
            )


def group_by_sensor_and_date(
    item: Item,
    parsed: ParsedItem,
    idx: int,
) -> str:
    id_split = item.id.split(".")
    sensor = id_split[1]
    day = id_split[3][:7]

    return f"{sensor}_{day}"

def filter_cloud(items, lim=90, start=0, inc=5, n=100):
    ''' start at eo:cloud_cover=start, increment by inc
    until n items are found or lim is reached'''
    stats = dict()
    for cc in range(start, lim+inc, inc):
        filtered_items = [i for i in items
                          if i.properties['eo:cloud_cover'] < cc]
        stats[cc] = len(filtered_items)
        if len(filtered_items) >= n:
            break

    logger.info(
        f'returning {len(filtered_items)} at eo_cloud_cover {cc} '
        f'cc stats: {stats}'
    )
    return filtered_items


def get_stac_items(
    bbox: BBox, start_datetime: datetime, end_datetime: datetime, crs: CRS,
    lim: int = None
) -> list[Item]:
    logger.info("querying HLS archive")
    client = DuckdbClient(
        use_hive_partitioning=True,
        extension_directory=DUCKDB_EXTENSION_DIRECTORY,
    )
    client.execute(
        """
        CREATE OR REPLACE SECRET secret (
             TYPE S3,
             PROVIDER CREDENTIAL_CHAIN
        );
        """
    )

    items = []
    for collection in HLS_COLLECTIONS:
        items.extend(
            client.search(
                href=HLS_STAC_GEOPARQUET_HREF.format(collection=collection),
                datetime="/".join(
                    dt.isoformat() for dt in [start_datetime, end_datetime]
                ),
                bbox=transform_bounds(
                    src_crs=crs,
                    dst_crs="epsg:4326",
                    left=bbox[0],
                    bottom=bbox[1],
                    right=bbox[2],
                    top=bbox[3],
                ),
                filter={
                    "op": "and",
                    "args": [
                        {
                            "op": "between",
                            "args": [
                                {"property": "year"},
                                start_datetime.year,
                                end_datetime.year,
                            ],
                        },
                    ],
                },
            )
        )

    logger.info(f"found {len(items)} items")
    all_items = [Item.from_dict(item) for item in items]

    if lim:
        return filter_cloud(all_items, n=lim)

    return all_items

def write_assets_and_catalog(bands, composite_type, composite, output_dir, start_datetime, end_datetime):
    assets = {}
    for band in bands:
        href = f"{band}.tif"
        logger.info(f"exporting {href}")
        da = composite[band]
        da.rio.set_nodata(NODATA, inplace=True)
        da_to_export = da.rio.write_nodata(NODATA, encoded=True, inplace=False)

        output_file_path = output_dir / href

        da_to_export.rio.to_raster(
            output_file_path,
            driver="COG",
            dtype='float32',
            compress="DEFLATE",
        )

        assets[band] = Asset(
            href=href,
            description=f"{composite_type} {band} band value from cloud-free pixels in the temporal mosaic",
            media_type=MediaType.COG,
            roles=["data"],
        )

    catalog = Catalog(
        id="DPS",
        description="DPS",
        catalog_type=CatalogType.SELF_CONTAINED,
    )

    # use one of the output files as a template for rio-stac
    source_file = f"{output_dir}/{assets[bands[0]].href}"

    item = create_stac_item(
        source=source_file,
        id="-".join(
            [
                "_".join(str(int(x)) for x in bbox),
                start_datetime.strftime("%Y%m%d"),
                end_datetime.strftime("%Y%m%d"),
            ]
        ),
        with_proj=True,
        properties={
            "datetime": end_datetime.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "start_datetime": start_datetime.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_datetime": end_datetime.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    )

    # replace auto-generated assets with our own
    item.assets = assets

    item.set_self_href(f"{output_dir}/item.json")

    # finalize catalog and save to the output directory
    catalog.add_item(item)
    item.make_asset_hrefs_relative()

    catalog.normalize_and_save(
        root_href=str(output_dir),
        catalog_type=CatalogType.SELF_CONTAINED,
    )

def write_multiband_raster(composite, output_dir, output_name):
    for band in list(composite.data_vars):
        composite[band].rio.set_nodata(NODATA, inplace=True)
        composite[band].rio.write_nodata(NODATA, encoded=True, inplace=True)

    ordered_bands = ['blue', 'green', 'red', 'nir', 'swir_1', 'swir_2',
                     'nbr', 'Fmask']

    select_bands = [x for x in ordered_bands if x in composite.data_vars]
    select_bands += [x for x in composite.data_vars if x not in ordered_bands]

    composite[select_bands].rio.to_raster(
        output_dir / output_name,
        driver="COG",
        compress="DEFLATE",
        dtype='float32',
    )



async def run(
    start_datetime: datetime,
    end_datetime: datetime,
    bbox: BBox,
    crs: CRS,
    output_dir: Path,
    bands: list[str] = DEFAULT_BANDS,
    resolution: int | float = DEFAULT_RESOLUTION,
    direct_bucket_access: bool = False,
    method: str = 'median',
    q: float = 0.98,
    lim: int = None,
    catalog: bool = False,
    output_name: str = None,
    indices: list[str] = None,
):
    items = get_stac_items(
        bbox=bbox,
        start_datetime=start_datetime,
        end_datetime=end_datetime,
        crs=crs,
        lim=lim
    )

    rasterio_env = {}
    if direct_bucket_access:
        maap = MAAP(maap_host="api.maap-project.org")
        creds = maap.aws.earthdata_s3_credentials(
            "https://data.lpdaac.earthdatacloud.nasa.gov/s3credentials"
        )
        odc.stac.configure_rio(
            cloud_defaults=True,
            aws={
                "aws_access_key_id": creds["accessKeyId"],
                "aws_secret_access_key": creds["secretAccessKey"],
                "aws_session_token": creds["sessionToken"],
                "region_name": "us-west-2",
            },
        )
        rasterio_env["session"] = AWSSession(
            **{
                "aws_access_key_id": creds["accessKeyId"],
                "aws_secret_access_key": creds["secretAccessKey"],
                "aws_session_token": creds["sessionToken"],
                "region_name": "us-west-2",
            }
        )
        for item in items:
            for asset in item.assets.values():
                if asset.href.startswith(URL_PREFIX):
                    asset.href = asset.href.replace(URL_PREFIX, "s3://")

    logger.info("checking proj metadata")
    fixed_count = 0
    with rasterio.Env(**rasterio_env):
        for item in items:
            if (not item.ext.proj.shape) and (not item.ext.proj.transform):
                fixed_count += 1
                with rasterio.open(item.assets["Fmask"].href) as src:
                    item.ext.proj.shape = src.shape
                    item.ext.proj.transform = list(src.transform)

    logger.info(f"fixed proj metadata for {fixed_count} items")

    logger.info("loading into xarray via odc.stac")
    stack = odc.stac.load(
        items,
        stac_cfg=HLS_ODC_STAC_CONFIG,
        bands=list(set(bands + ["Fmask"])),
        chunks={"x": 512, "y": 512},
        groupby=group_by_sensor_and_date,
        geobox=GeoBox.from_bbox(bbox=bbox, crs=crs, resolution=resolution, tight=True),
    ).sortby("time")
    logger.info(f"{stack.info()}\n{stack.chunk()}")


    stack = mask_and_scale(stack, bands)

    logger.info("computing composite values")
    composite = calculate_composite(stack, indices if indices else [], method, q)


    if catalog:
        write_assets_and_catalog(
            bands,
            composite_type,
            composite,
            output_dir,
            start_datetime,
            end_datetime
        )
    else:
        write_multiband_raster(composite, output_dir, output_name)


if __name__ == "__main__":
    parse = argparse.ArgumentParser(
        description="Queries the HLS STAC geoparquet archive and writes the result to a file"
    )
    parse.add_argument(
        "--start_datetime",
        help="start datetime in ISO format (e.g., 2024-01-01T00:00:00Z)",
        required=True,
        type=str,
    )
    parse.add_argument(
        "--end_datetime",
        help="end datetime in ISO format (e.g., 2024-12-31T23:59:59Z)",
        required=True,
        type=str,
    )
    parse.add_argument(
        "--bbox",
        help="bounding box (xmin, ymin, xmax, ymax)",
        required=False,
        nargs=4,
        type=float,
        metavar=("xmin", "ymin", "xmax", "ymax"),
    )
    parse.add_argument(
        "--crs",
        help="CRS definition of the bounding box coordinates",
        required=False,
        type=str,
    )
    parse.add_argument(
        "--output_dir", help="Directory in which to save output", required=True
    )
    parse.add_argument(
        "--output_name",
        help="cog name if writing a multiband raster",
        required=False
    )
    parse.add_argument(
        "--direct_bucket_access",
        help="Use direct S3 bucket access instead of HTTP URLs",
        action="store_true",
        default=False,
    )
    parse.add_argument(
        "--composite_type",
        help="options are median, maxndvi, qndvi. If qndvi, --q must be given too.",
        default='median',
    )
    parse.add_argument(
        "--q",
        type=float,
        default=0.98,
        help=("Quantile of ndvi along the time dim, must be a float in [0,1]."
              "Default %(default)s"),
    )
    parse.add_argument(
        "--aoi",
        help="vector file area of interest",
        required=False,
        type=str,
    )
    parse.add_argument(
        "--lim",
        help="Limit the number of stac items",
        required=False,
        type=int,
        default=None
    )
    parse.add_argument(
        "--catalog",
        help=("catalog and write individual stac assets to disk, "
              "otherwise write a multiband raster"),
        required=False,
        action="store_true",
        default=False
    )
    parse.add_argument(
        "--indices",
        help=(f"space separated list of any of {INDEX_FUNS.keys()}"),
        required=False,
        nargs='*',
        type=str,
        default=[]
    )


    args = parse.parse_args()

    output_dir = Path(args.output_dir)
    if args.bbox:
        bbox = tuple(args.bbox)
        crs = CRS.from_string(args.crs)
        validate_crs_units_in_meters(crs)
    elif args.aoi:
        import fiona
        with fiona.open(args.aoi) as src:
            profile = src.profile
            crs = src.crs
            bbox = src.bounds
    else:
        raise ValueError('Either aoi or (bbox and crs) must be provided.')

    if not args.catalog and not args.output_name:
        raise ValueError('Either write and catalog assets [--catalog] or write '
                         'multiband tif [--output_name]')

    start_datetime = parse_datetime_utc(args.start_datetime)
    end_datetime = parse_datetime_utc(args.end_datetime)
    if args.composite_type not in ('maxndvi', 'qndvi', 'median'):
        raise ValueError('composite_type must be one of maxndvi, qndvi, median')

    if not set(args.indices) <= INDEX_FUNS.keys():
        raise ValueError(f'--indices must be a subset of {INDEX_FUNS.keys()}')

    logging.info(
        f"setting GDAL config environment variables:\n{json.dumps(GDAL_CONFIG, indent=2)}"
    )
    os.environ.update(GDAL_CONFIG)

    logging.info(f'{args}')

    # Retry loop for handling intermittent failures
    max_retries = 3
    retry_delay = 5  # seconds

    for attempt in range(max_retries):
        try:
            asyncio.run(
                run(
                    start_datetime=start_datetime,
                    end_datetime=end_datetime,
                    bbox=bbox,
                    crs=crs,
                    output_dir=output_dir,
                    direct_bucket_access=args.direct_bucket_access,
                    method=args.composite_type,
                    q=args.q,
                    lim=args.lim,
                    catalog=args.catalog,
                    output_name=args.output_name,
                    indices=args.indices,
                )
            )
            logging.info("Successfully completed processing")
            break
        except Exception as e:
            if attempt < max_retries - 1:
                wait_time = retry_delay * (2**attempt)  # exponential backoff
                logging.warning(
                    f"Attempt {attempt + 1}/{max_retries} failed with error: {e}. "
                    f"Retrying in {wait_time} seconds..."
                )
                time.sleep(wait_time)
            else:
                logging.error(f"All {max_retries} attempts failed. Last error: {e}")
                raise
