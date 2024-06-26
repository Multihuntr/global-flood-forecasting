"""
We need daily-aggregated ERA5-Land which is not provided natively from CDS.
Theoretically, we could download the ERA5-Land hourly archive and reprocess to daily values.
But the whole archive is terrabytes large, the api has strong rate limits and we don't need hourly
(at least, not everywhere all the time). Instead - presumably because of prior research -
a daily reprocessed version is on Google Earth Engine.

We have some preferences/considerations:
1. We'd rather not be locked into using earth engine
2. We want to do arbitrary training on our machines
3. We have limited disk space.
4. We looked to NeuralHydrology streamflow models for inspiration, and these are from Google.

Thus, we decided to use `earthengine-api` to export the ERA5-Land files from GEE to a Drive and 
then download that file using `google-api-python-client` (which comes with `earthengine-api`).
This is that script.
"""

import argparse
import contextlib
import functools
import logging
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import ee
import google.oauth2.service_account
import googleapiclient.discovery
import googleapiclient.http
import numpy as np
import shapely
import rasterio
import rasterio.mask
import rasterio.merge

import gff.constants

ssl_lock = threading.Lock()
ram_lock = threading.Lock()
task_times = []


def _export_to_gdrive(img, fname):
    ssl_lock.acquire()
    task = ee.batch.Export.image.toDrive(img, description=fname)
    task.start()
    status = task.status()
    ssl_lock.release()
    start = time.time()
    while status["state"] in ["UNSUBMITTED", "PENDING", "READY", "RUNNING"]:
        time.sleep(20)
        now = time.time()
        ssl_lock.acquire()
        status = task.status()
        ssl_lock.release()
        logging.debug(f'[{fname}] {status["state"]}: Elapsed {int(now-start):10d}s')
    logging.debug(status)
    if status["state"] in ["CANCELLING", "CANCELLED", "FAILED"]:
        raise Exception("Task was killed/failed")
    task_times.append(now - start)
    return now - start


@contextlib.contextmanager
def log_timing(fmt):
    start = time.perf_counter()
    yield
    total = time.perf_counter() - start
    logging.info(fmt.format(time=total))


def _get_files(gservice, fname):
    ssl_lock.acquire()
    response = gservice.files().list(q=f"name contains '{fname}'", spaces="drive").execute()
    gfiles = response.get("files", [])
    ssl_lock.release()
    return gfiles


def _download_by_fname(gservice, fname):
    logging.info(f"[{fname}] Downloading.")

    # Check files on drive
    gfiles = _get_files(gservice, fname)
    assert len(gfiles) > 0, "File has not been created."

    file_pointers = []
    with log_timing("Time to download {time:5.2f}s"):
        for gfile in gfiles:
            # Download a file from Google Drive to temporary file
            ssl_lock.acquire()
            export_request = gservice.files().get_media(fileId=gfile["id"])
            file_pointer = tempfile.NamedTemporaryFile("w+b")
            downloader = googleapiclient.http.MediaIoBaseDownload(file_pointer, export_request)
            done = False
            while done is False:
                status, done = downloader.next_chunk()
                logging.debug("Download %d%%" % int(status.progress() * 100))

            # Delete from Google Drive
            gservice.files().delete(fileId=gfile["id"]).execute()
            ssl_lock.release()

            # Add to list of file_pointers to be used elsewhere (don't close yet!)
            file_pointer.seek(0)
            file_pointers.append(file_pointer)

    return file_pointers


def _reprocess_to_16bit(file_pointers, out_fpath, new_band_names):
    # Merge tifs into one big one
    tifs = [rasterio.open(fp) for fp in file_pointers]

    with log_timing("Merging took {time:5.2f}s"):
        bands, transform = rasterio.merge.merge(tifs, nodata=np.nan)

    out_profile = {
        **tifs[0].profile,
        "height": bands.shape[1],
        "width": bands.shape[2],
        "transform": transform,
        "dtype": "int16",
        "nodata": -32767,
        "BIGTIFF": "YES",
        "TILEXSIZE": 48,
        "TILEYSIZE": 48,
    }
    with rasterio.open(out_fpath, "w", **out_profile) as out_f:
        band_idxs = list(range(1, 1 + tifs[0].count))
        areamask, _, _ = rasterio.mask.raster_geometry_mask(out_f, SANS_ANTARCTICA)

        # Convert to int16 representation and set nodata
        with log_timing("Processing took {time:5.2f}s"):
            bands_int = np.ones(bands.shape, dtype=np.int16) * -32767
            offsets = np.nanmin(bands, axis=(1, 2), keepdims=True)
            scales = (np.nanmax(bands, axis=(1, 2), keepdims=True) - offsets) / 2**14
            nanmask = np.isnan(bands)
            mask = ~nanmask & ~areamask
            bands_int[mask] = ((bands - offsets) / scales)[mask].astype(np.int16)

        # Write to new file as int16 with scale/offset
        out_f.scales = tuple(scales.flatten())
        out_f.offsets = tuple(offsets.flatten())
        with log_timing("Writing to disk: {time:5.2f}s"):
            out_f.write(bands_int, band_idxs)

        # Give each band a proper name - relying on order being preserved throughout
        new_band_names_flat = [e for img_band_names in new_band_names for e in img_band_names]
        for band_idx, band_name in enumerate(new_band_names_flat):
            out_f.set_band_description(band_idx + 1, band_name)
    for tif in tifs:
        tif.close()


def estimate_remaining_time(n_tasks):
    average_time = np.array(task_times).mean()
    expected_total_s = average_time * n_tasks / 1000
    expected_hours = expected_total_s / 3600
    expected_mins = (expected_total_s % 3600) / 60
    logging.info(f"TIME ESTIMATE: {expected_hours:6d}hrs and {expected_mins:6d}mins ")


def merge_dataset(dataset, new_band_names):
    size = dataset.size().getInfo()
    dataset_list = dataset.toList(size, 0)
    imgs = []
    for idx in range(size):
        img = ee.Image(dataset_list.get(idx)).rename(new_band_names[idx])
        imgs.append(img)

    combined = imgs[0].addBands(imgs[1:])
    return combined


def get_new_band_names(dataset, prefix, band_names):
    size = dataset.size().getInfo()
    year_month_band_names = [
        [f"{prefix}-{(idx+1):02d}-{name}" for name in band_names] for idx in range(size)
    ]
    return year_month_band_names


def download_locally(year, month, dataset, gservice, local_folder, band_names, prefix):
    # Determine date range
    start_date = f"{year:04d}-{month:02d}"
    fname = f"{prefix}-{start_date}"
    local_fpath = local_folder / f"{fname}.tif"
    if local_fpath.exists():
        logging.info(f"[{fname}] Found locally; skipping entirely.")
        return
    end_year = year + (month // 12)
    end_month = (month % 12) + 1
    end_date = f"{end_year:04d}-{end_month:02d}"
    filtered_dataset = dataset.filterDate(start_date, end_date)
    new_band_names = get_new_band_names(filtered_dataset, start_date, band_names)

    # Submit export job to google and wait until done
    existing_files = _get_files(gservice, fname)
    if len(existing_files) > 0:
        logging.info(f"[{fname}] Found on google drive; skipping request for export.")
    else:
        logging.info(f"[{fname}] Requesting export.")
        img = merge_dataset(filtered_dataset, new_band_names)
        _export_to_gdrive(img, fname)
        logging.info(f"[{fname}] Export complete.")

    # google-api-python-client
    file_pointers = _download_by_fname(gservice, fname)

    # Reprocess to 16-bit numbers
    ram_lock.acquire()
    _reprocess_to_16bit(file_pointers, local_fpath, new_band_names)
    ram_lock.release()

    # Ensure file pointers are closed properly
    for file_pointer in file_pointers:
        file_pointer.close()
    logging.info(f"[{fname}] Finished.")


def parse_args():
    parser = argparse.ArgumentParser("Download ERA5-Land from earthengine")

    parser.add_argument(
        "service_account_name",
        type=str,
        help="Google cloud service account name (e.g. xxx@<project>.iam.gserviceaccount.com)",
    )
    parser.add_argument(
        "service_account_private_key_path",
        type=str,
        help="Path to private key downloaded from google cloud.",
    )
    parser.add_argument("folder", type=Path, help="folder to save tifs to")
    parser.add_argument(
        "--n_concurrent", type=int, default=3, help="how many concurrent requests to run"
    )
    parser.add_argument(
        "--not_land", action="store_true", help="default: ERA5-Land, if provided, download ERA5"
    )

    return parser.parse_args()


# ERA5Land bands from:
#  https://github.com/kratzert/Caravan/blob/4ef0dc13052ada53968be43b008738cb8335e31b/code/Caravan_part1_Earth_Engine.ipynb#L873
# Note that we are downloading post-processed version,
# and some band names have "_sum" appended to the end
START_DATE = "2014-01-01"  # Format: YYYY-MM-DD
END_DATE = "2023-01-01"  # Format: YYYY-MM-DD
YEAR_RANGE = range(2013, 2024)
MONTH_RANGE = range(1, 13)
SANS_ANTARCTICA = shapely.polygons([[[-179, 85], [179, 85], [179, -60], [-179, -60]]])


def main(args, gservice):
    if not (args.not_land):
        band_names = gff.constants.ERA5L_BANDS
        prefix = "era5-land"
        dataset = ee.ImageCollection("ECMWF/ERA5_LAND/DAILY_AGGR").select(
            *gff.constants.ERA5L_BANDS
        )
    else:
        band_names = gff.constants.ERA5_BANDS
        prefix = "era5"
        dataset = ee.ImageCollection("ECMWF/ERA5/DAILY").select(*gff.constants.ERA5_BANDS)

    _download_locally_partial = functools.partial(
        download_locally,
        dataset=dataset,
        gservice=gservice,
        local_folder=args.folder,
        band_names=band_names,
        prefix=prefix,
    )

    with ThreadPoolExecutor(max_workers=args.n_concurrent) as executor:
        futures = []
        for year in YEAR_RANGE:
            for month in MONTH_RANGE:
                futures.append(executor.submit(_download_locally_partial, year, month))

        for future in futures:
            future.result()


if __name__ == "__main__":
    args = parse_args()
    # To make this work, I created a google cloud project, and registered it as a
    # google earth engine research project.

    # Instructions:
    # Go to: https://console.cloud.google.com
    # Create a project
    # Go to: https://console.cloud.google.com/apis/credentials
    # "Create Credentials" -> "Service Account"
    # You don't need to add any extra stuff to it.
    # Select the service account -> "Keys" tab near top -> "New Key"
    # Save the json to your computer and provide it via 'service_account_private_key_path'
    ee_creds = ee.ServiceAccountCredentials(
        args.service_account_name, args.service_account_private_key_path
    )
    ee.Initialize(ee_creds)

    gapi_creds = google.oauth2.service_account.Credentials.from_service_account_file(
        filename=args.service_account_private_key_path,
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    gservice = googleapiclient.discovery.build("drive", "v3", credentials=gapi_creds)

    # Set up logging
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    main(args, gservice)
