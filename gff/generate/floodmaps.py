import datetime
import functools
import itertools
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import traceback
from typing import Union
import warnings

import affine
import geopandas
import numpy as np
import pandas as pd
import rasterio
import scipy
import shapely
import skimage
import torch

from gff import constants
from gff import data_sources
from gff import util
import gff.generate.util


def check_tifs_match(tifs):
    transforms = [tif.transform for tif in tifs]
    t0 = transforms[0]
    res = t0[0], t0[4]
    assert all(
        [np.allclose((t[0], t[4]), res) for t in transforms[1:]]
    ), "Not all tifs have same resolution"
    assert all([tif.crs == tifs[0].crs for tif in tifs[1:]]), "Not all tifs have the same CRS"


def ensure_s1(data_folder, prefix, results, delete_intermediate=False):
    img_folder = data_folder / "s1"
    d = datetime.datetime.fromisoformat(results[0]["properties"]["startTime"])
    d_str = d.strftime("%Y-%m-%d")
    filename = f"{prefix}-{d_str}.tif"
    out_fpath = img_folder / filename
    if out_fpath.exists():
        return out_fpath

    zip_fpaths, processed_fpaths = [], []
    for result in results:
        # Download image
        zip_fpath = data_sources.download_s1(img_folder, result)
        zip_fpaths.append(zip_fpath)

        # Run preprocessing on whole image
        dim_fname = Path(zip_fpath.with_name(zip_fpath.stem + "-processed.dim").name)
        data_sources.preprocess_s1(data_folder, zip_fpath.name, dim_fname)
        # DIMAP is a weird format. You ask it to save at ".dim" and it actually saves elsewhere
        # Anyway, need to combine the vv and vh bands into a single file for later merge
        dim_data_path = img_folder / "tmp" / dim_fname.with_suffix(".data")
        processed_fpath = img_folder / dim_fname.with_suffix(".tif")
        processed_fpaths.append(processed_fpath)
        if not processed_fpath.exists():
            print("Merging DIMAP to a single TIF.")
            subprocess.run(
                [
                    "gdal_merge.py",
                    "-separate",
                    "-o",
                    processed_fpath,
                    dim_data_path / "Sigma0_VV.img",
                    dim_data_path / "Sigma0_VH.img",
                ]
            )
            if delete_intermediate:
                shutil.rmtree(dim_data_path)
                dim_data_path.with_suffix(".dim").unlink()

    if len(processed_fpaths) > 1:
        print("Merging multiple captures from the same day into a single tif")
        subprocess.run(["gdal_merge.py", "-n", "0", "-o", out_fpath, *processed_fpaths])
    else:
        shutil.copy(processed_fpaths[0], out_fpath)

    print("Compressing result")
    tmp_compress_path = img_folder / "tmp" / filename
    compress_args = ["-co", "COMPRESS=LERC", "-co", "MAX_Z_ERROR=0.0001"]
    util_args = ["-co", "BIGTIFF=YES", "-co", "INTERLEAVE=BAND"]
    subprocess.run(["gdal_translate", *compress_args, *util_args, out_fpath, tmp_compress_path])
    shutil.move(tmp_compress_path, out_fpath)

    if delete_intermediate:
        for p in processed_fpaths:
            p.unlink()
    return out_fpath


def floodmap_meta_fpaths(data_folder, cross_term, floodmap_folder):
    folder = data_folder / "floodmaps"
    if floodmap_folder is not None:
        folder = folder / floodmap_folder

    return list((folder).glob(f"{cross_term}-*-meta.json"))


def check_floodmap_exists(data_folder, cross_term, floodmap_folder):
    fpaths = floodmap_meta_fpaths(data_folder, cross_term, floodmap_folder)
    return len(fpaths) >= 1


def load_metas(data_folder, cross_term, floodmap_folder):
    fpaths = floodmap_meta_fpaths(data_folder, cross_term, floodmap_folder)
    fpaths.sort()
    metas = []
    for fpath in fpaths:
        with open(fpath) as f:
            metas.append(json.load(f))
    return metas


def floodmap_tiles_from_meta(meta_path: Path):
    with open(meta_path) as f:
        meta = json.load(f)
    v_path = meta_path.parent / meta["visit_tiles"]
    visit_tiles = geopandas.read_file(v_path, engine="pyogrio", use_arrow=True)
    return np.array(visit_tiles.geometry)


def create_flood_maps(
    data_folder,
    search_results,
    basin_row,
    rivers_df,
    flood_model,
    export_s1=True,
    n_img=3,
    floodmap_folder=None,
    search_idxs=None,
    prescribed_tiles=None,
):
    """
    Creates flood maps for a single basin x flood shape by starting along rivers
    and expanding the flood maps until there's no more flood, or the edge of the image
    is reached.

    Will download S1 images as needed. Since the flood time window is quite long, and we
    don't know when the flood actually happened, more than three S1 images may be downloaded.
    The flood_model is used to decide whether a flood is visible.

    This will stop when flooding is found, or we run out of sentinel images.
    """
    cross_term = f"{basin_row.ID}-{basin_row.HYBAS_ID}"
    print(f"Generating floodmaps for  {cross_term}")

    # How to choose combinations of S1? Who knows? *shrug*
    # Pick the first triplet, then, if no flooding, choose the next triplet
    msg = "Cannot prescribe tiles if search_idxs are not also prescribed"
    assert (search_idxs is None and prescribed_tiles is None) or search_idxs is not None, msg
    if search_idxs is None:
        search_idxs = gff.generate.util.find_intersecting_tuplets(search_results, basin_row, n_img)
        if search_idxs is None:
            print("No overlap between S1 images; exiting early. No floodmaps generated.")
            return

    metas = []
    for map_idx, search_idx in enumerate(search_idxs):
        results_group = [search_results[i] for i in search_idx]

        s1_img_paths = []
        date_strs = []
        for res in results_group:
            # Get image
            img_path = ensure_s1(data_folder, cross_term, res, delete_intermediate=True)
            s1_img_paths.append(img_path)
            # Get image date
            res_date = datetime.datetime.fromisoformat(res[0]["properties"]["startTime"])
            date_strs.append(res_date.strftime("%Y-%m-%d"))

        print(f"Generating for  {cross_term}  using S1 from   {'  '.join(date_strs)}")

        tif_filename = Path(f"{cross_term}-{'-'.join(date_strs)}.tif")
        meta_filename = tif_filename.with_name(tif_filename.stem + "-meta.json")
        visit_filename = tif_filename.with_name(tif_filename.stem + "-visit.gpkg")
        flood_filename = tif_filename.with_name(tif_filename.stem + "-flood.gpkg")
        if floodmap_folder is not None:
            abs_floodmap_folder = data_folder / "floodmaps" / floodmap_folder
        else:
            abs_floodmap_folder = data_folder / "floodmaps"
        abs_floodmap_folder.mkdir(exist_ok=True)
        viable_footprint = gff.generate.util.search_result_footprint_intersection(results_group)
        if prescribed_tiles is not None:
            this_prescribed_tiles = prescribed_tiles[map_idx]
        else:
            this_prescribed_tiles = None
        visit_tiles, tile_stats, n_flooded, s1_export_paths = progressively_grow_floodmaps(
            s1_img_paths,
            rivers_df,
            data_folder,
            abs_floodmap_folder / tif_filename,
            flood_model,
            export_s1=export_s1,
            print_freq=200,
            viable_footprint=viable_footprint,
            prescribed_tiles=this_prescribed_tiles,
        )

        meta = {
            "type": "generated",
            "key": cross_term,
            "FLOOD": f"{basin_row.ID}",
            "HYBAS_ID": f"{basin_row.HYBAS_ID}",
            "pre2_date": search_results[search_idx[0]][0]["properties"]["startTime"],
            "pre1_date": search_results[search_idx[1]][0]["properties"]["startTime"],
            "post_date": search_results[search_idx[2]][0]["properties"]["startTime"],
            "floodmap": str(tif_filename),
            "visit_tiles": str(visit_filename),
            "n_flooded": n_flooded,
        }
        if export_s1:
            meta["s1"] = [str(p.relative_to(data_folder)) for p in s1_export_paths]

        if n_flooded < 50:
            print(" No major flooding found.")
            meta["flooding"] = False
        else:
            print(" Major flooding found.")
            meta["flooding"] = True

        if export_s1:
            for i, s1_export_path in enumerate(s1_export_paths):
                with rasterio.open(s1_export_path, "r+") as s1_tif:
                    desc_keys = ["flightDirection", "pathNumber", "startTime", "orbit"]
                    props = search_results[search_idx[i]][0]["properties"]
                    desc_dict = {k: props[k] for k in desc_keys}
                    s1_tif.update_tags(1, **desc_dict, polarisation="vv")
                    s1_tif.update_tags(2, **desc_dict, polarisation="vh")
                    s1_tif.descriptions = ["vv", "vh"]

        with (abs_floodmap_folder / meta_filename).open("w") as f:
            json.dump(meta, f)
        metas.append(meta)

        fpath = abs_floodmap_folder / visit_filename
        util.save_tiles(visit_tiles, tile_stats, fpath, "EPSG:4326")
        if n_flooded >= 50:
            break

    print(f"Completed floodmaps for {cross_term}.")

    return metas


def progressively_grow_floodmaps(
    inp_img_paths: list[Path],
    rivers_df: geopandas.GeoDataFrame,
    data_folder: Path,
    floodmap_path: Path,
    flood_model: Union[torch.nn.Module, callable],
    tile_size: int = 224,
    export_s1: bool = False,
    print_freq: int = 0,
    viable_footprint: shapely.Geometry = None,
    max_tiles: int = 2500,
    prescribed_tiles: np.ndarray[shapely.Geometry] = None,
):
    """
    First runs flood_model along riverways, then expands the search as flooded areas are found.
    Stores all visited tiles to floodmap_path.

    Note: all tifs in inp_img_paths must match resolution and CRS.
    Thus the output is in the same CRS.

    The output will be aligned pixel-wise with the last input image, and the other inputs will
    be resampled to match
    """
    # Open and check tifs
    inp_tifs = [rasterio.open(p) for p in inp_img_paths]
    ref_tif = inp_tifs[-1]
    check_tifs_match(inp_tifs)

    # Create tile_grid and initial set of tiles
    tile_grids, footprint_box = mk_grid(viable_footprint, ref_tif.transform, tile_size)
    grid_size = tile_grids[(0, 0)].shape
    if prescribed_tiles is None:
        tiles = create_initial_tiles(
            rivers_df,
            viable_footprint,
            tile_grids[(0, 0)],
            ref_tif.crs,
            min_river_size=500,
            max_tiles=200,
        )
    else:
        tiles = get_grid_idx(tile_grids[(0, 0)], prescribed_tiles)

    visited = np.zeros_like(tile_grids[(0, 0)]).astype(bool)
    offset_cache = {(0, 1): {}, (1, 0): {}, (1, 1): {}}

    # Rasterio profile handling
    outxlo, _, _, outyhi = footprint_box.bounds
    t = ref_tif.transform
    new_transform = rasterio.transform.from_origin(outxlo, outyhi, t[0], -t[4])
    floodmap_nodata = 255
    profile = {
        **ref_tif.profile,
        **constants.FLOODMAP_PROFILE_DEFAULTS,
        "nodata": floodmap_nodata,
        "transform": new_transform,
        "width": tile_grids[0, 0].shape[0] * tile_size,
        "height": tile_grids[0, 0].shape[1] * tile_size,
        "BIGTIFF": "IF_NEEDED",
    }
    s1_fpaths = None
    s1_tifs = None
    if export_s1:
        s1_profile = {
            **ref_tif.profile,
            **constants.S1_PROFILE_DEFAULTS,
            "transform": new_transform,
            "width": tile_grids[0, 0].shape[0] * tile_size,
            "height": tile_grids[0, 0].shape[1] * tile_size,
            "BIGTIFF": "YES",
        }
        s1_folder = data_folder / "s1-export"
        s1_folder.mkdir(exist_ok=True)
        s1_tifs = []
        s1_fpaths = []
        for name in constants.KUROSIWO_S1_NAMES[-(len(inp_img_paths)) :]:
            s1_fpath = s1_folder / f"{floodmap_path.stem}_{name}.tif"
            s1_fpaths.append(s1_fpath)
            if s1_fpath.exists():
                s1_tifs.append(rasterio.open(s1_fpath, "r+"))
            else:
                s1_tifs.append(rasterio.open(s1_fpath, "w", **s1_profile))

    # Begin flood-fill search
    visit_tiles, fill_tiles = [], []
    geom_stats = []
    raw_floodmap_path = floodmap_path.with_stem(floodmap_path.stem + "-raw")
    with rasterio.open(raw_floodmap_path, "w", **profile) as out_tif:
        n_visited = 0
        n_flooded = 0
        n_permanent = 0
        n_outside = 0
        while len(tiles) > 0 and len(visit_tiles) < max_tiles:
            # Grab a tile and get logits
            tile_x, tile_y = tiles.pop(0)
            visited[tile_x, tile_y] = True
            n_visited += 1
            tile_geom = tile_grids[(0, 0)][tile_x, tile_y]
            visit_tiles.append(tile_geom)
            if not shapely.contains(viable_footprint, tile_geom):
                n_outside += 1
                continue
            s1_inps, dem, flood_logits = flood_model(inp_tifs, tile_geom)
            s1_inps = [t[0].cpu().numpy() for t in s1_inps]
            if not s1_preprocess_edge_heuristic(s1_inps) or flood_logits is None:
                n_outside += 1
                continue

            # Smooth logits out over adjacent tiles
            adjacent_logits = get_adjacent_logits(
                inp_tifs, tile_grids, tile_x, tile_y, flood_model, offset_cache
            )
            flood_logits = average_logits_towards_edges(flood_logits, adjacent_logits)

            # Write classes to disk
            flood_cls = flood_logits.argmax(axis=0)[None].astype(np.uint8)
            window = util.shapely_bounds_to_rasterio_window(tile_geom.bounds, out_tif.transform)
            if export_s1:
                for s1_tif, s1_inp_tile in zip(s1_tifs, s1_inps):
                    s1_tif.write(s1_inp_tile, window=window)
            out_tif.write(flood_cls, window=window)
            fill_tiles.append(tile_geom)
            stats = data_sources.ks_water_stats(flood_cls)
            geom_stats.append(stats)

            # Select new potential tiles (if not visited)
            if ((flood_cls == constants.KUROSIWO_PW_CLASS).mean() > 0.5) or (
                (flood_cls == constants.KUROSIWO_BG_CLASS).mean() < 0.1
            ):
                # Don't go into the ocean or large lakes
                n_permanent += 1
            elif tile_flooded(stats):
                n_flooded += 1
                new_tiles = sel_new_tiles_big_window(tile_x, tile_y, *grid_size, add=3)
                for tile in new_tiles:
                    if not visited[tile] and (tile not in tiles):
                        tiles.append(tile)

            # Logging
            if print_freq > 0 and n_visited % print_freq == 0 or len(tiles) == 0:
                print(
                    f"{len(tiles):6d} open",
                    f"{n_visited:6d} visited",
                    f"{n_flooded:6d} flooded",
                    f"{n_permanent:6d} in large bodies of water",
                    f"{n_outside:6d} outside legal bounds",
                )
        print(
            f"{len(tiles):6d} open",
            f"{n_visited:6d} visited",
            f"{n_flooded:6d} flooded",
            f"{n_permanent:6d} in large bodies of water",
            f"{n_outside:6d} outside legal bounds",
        )
    if export_s1:
        for s1_tif in s1_tifs:
            s1_tif.close()
    for tif in inp_tifs:
        tif.close()

    print(" Tile search complete. Postprocessing outputs.")
    postprocess_classes(raw_floodmap_path, floodmap_path, floodmap_nodata)
    return fill_tiles, geom_stats, n_flooded, s1_fpaths


def remove_tiles_outside(meta_path: Path, basins_df: geopandas.GeoDataFrame):
    with meta_path.open() as f:
        meta = json.load(f)

    v_path = meta_path.parent / meta["visit_tiles"]
    visit_tiles = geopandas.read_file(v_path, engine="pyogrio", use_arrow=True)
    hybas_id, visit_mask = util.majority_tile_mask_for_basin(visit_tiles.geometry, basins_df)

    # Write nodata to tiles outside majority basin
    with rasterio.open(meta_path.parent / meta["floodmap"], "r+") as tif:
        for tile in visit_tiles.geometry.values[visit_mask]:
            tile_geom = shapely.Polygon(tile)
            window = util.shapely_bounds_to_rasterio_window(tile_geom.bounds, tif.transform)
            (yhi, ylo), (xhi, xlo) = window
            tif.write(
                np.full((tif.count, abs(yhi - ylo), abs(xhi - xlo)), tif.nodata), window=window
            )

    visit_tiles = visit_tiles[~visit_mask]
    visit_tiles.to_file(v_path, engine="pyogrio")

    meta["HYBAS_ID_4"] = hybas_id.item()
    with meta_path.open("w") as f:
        json.dump(meta, f)


def major_upstream_riverways(basins_df, start, bounds, threshold=20000):
    """Creates a shape that covers all the major upstream riverways within bounds"""
    upstream = util.get_upstream_basins(basins_df, start["HYBAS_ID"])
    major = upstream[upstream["riv_tc_usu"] > threshold]
    return shapely.intersection(major.convex_hull(), bounds)


def mk_grid(geom: shapely.Geometry, transform: affine.Affine, gridsize: int):
    """Create a grid in CRS-space where each block is `gridsize` large"""
    # Translate geom into some pixel-space coordinates
    geom_in_px = shapely.ops.transform(lambda x, y: ~transform * (x, y), geom)

    # So that you can create a grid that is the correct size in pixel coordinates
    xlo, ylo, xhi, yhi = geom_in_px.bounds
    # Since the geom potentially describes a validity boundary, and the grids are not
    # guaranteed to align with every other raster, pull in the boundary by one pixel
    # This ensures that resampling always has at least one pixel to play with
    xlo, ylo, xhi, yhi = (
        math.ceil(xlo) + 1,
        math.ceil(ylo) + 1,
        math.floor(xhi) - 1,
        math.floor(yhi) - 1,
    )
    w_px, h_px = (xhi - xlo), (yhi - ylo)
    s = gridsize
    grids = {
        (0, 0): util.mk_box_grid(w_px, h_px, xlo, ylo, s, s),
        (1, 0): util.mk_box_grid(w_px - s, h_px, xlo + s // 2, ylo, s, s),
        (0, 1): util.mk_box_grid(w_px, h_px - s, xlo, ylo + s // 2, s, s),
        (1, 1): util.mk_box_grid(w_px - s, h_px - s, xlo + s // 2, ylo + s // 2, s, s),
    }

    # Then translate grid back into CRS so that they align correctly across images
    grids = {k: util.convert_affine(grid, transform) for k, grid in grids.items()}
    # The geom is then aligned to the pixels in the reference tif to ensure no resampling
    new_geom = shapely.box(xlo, ylo, xhi, yhi)
    pixel_aligned_geom = shapely.ops.transform(
        lambda x, y: transform * np.array((x, y), dtype=np.float64), new_geom
    )

    # Note this only works if all images these grids are applied to have the exact same resolution
    return grids, pixel_aligned_geom


def tiles_along_river_within_geom(
    rivers_df, geom, tile_grid, crs, min_river_size=500, max_tiles=200
):
    """
    Select tiles from a tile_grid where the rivers (multiple LINEs) touch a geom (a POLYGON).
    """
    # River geoms within geom
    rivers_df = rivers_df.to_crs(crs)
    river = rivers_df[rivers_df.geometry.intersects(geom)]
    if len(river) == 0:
        return []

    # Combine river geoms and check for intersection with tile_grid
    if min_river_size is not None:
        river = river[river["riv_tc_usu"] > min_river_size]
    river = river.sort_values("riv_tc_usu")
    intersects = shapely.union_all(river.geometry.values).intersects(tile_grid)

    # Return the tile coordinates of intersection as a list
    x, y = intersects.nonzero()
    return list(zip(x, y))[:max_tiles]


def create_initial_tiles(rivers_df, geom, tile_grid, crs, min_river_size=500, max_tiles=200):
    tiles = tiles_along_river_within_geom(
        rivers_df,
        geom,
        tile_grid,
        crs,
        min_river_size=min_river_size,
        max_tiles=max_tiles,
    )
    if len(tiles) == 0:
        # This can happen if there's no rivers visible in geom.
        # First try with no restriction on river size
        tiles = tiles_along_river_within_geom(
            rivers_df,
            geom,
            tile_grid,
            crs,
            min_river_size=0,
            max_tiles=min_river_size,
        )
    gw, gh = grid_size = tile_grid.shape
    if len(tiles) == 0:
        # Then, if there's still no tiles, just use the centre of the footprint.
        tiles = sel_new_tiles_big_window(gw // 2, gh // 2, *grid_size, add=3)

    # Expand a small area around the river tiles
    for tile_x, tile_y in tiles.copy():
        new_tiles = sel_new_tiles_big_window(tile_x, tile_y, *grid_size, add=2)
        for tile in new_tiles:
            if tile not in tiles:
                tiles.append(tile)
    return tiles


def get_grid_idx(grid, tile_geoms):
    # grid shaped [W, H], tile_geoms shaped [N]
    tiles_combined = shapely.unary_union(tile_geoms)
    tile_area = grid[0, 0].area
    tiles_incl = shapely.area(shapely.intersection(grid, tiles_combined)) > (0.01 * tile_area)
    return list(zip(*tiles_incl.nonzero()))


def _ensure_logits(tifs, offset_tile_grid, x, y, flood_model, offset_cache):
    if (x, y) in offset_cache:
        return
    w, h = offset_tile_grid.shape
    if x >= 0 and y >= 0 and x < w and y < h:
        offset_tile_geom = offset_tile_grid[x, y]
        _, _, offset_logits = flood_model(tifs, offset_tile_geom)
        offset_cache[(x, y)] = offset_logits
    else:
        offset_cache[(x, y)] = None


def get_adjacent_logits(tifs, tile_grids, tile_x, tile_y, flood_model, offset_cache):
    """
    Given a set of tile_grids which are offset from one another by half a tile,
    run the flood model and add the tile to the offset_cache if it is not already there.
    Then return the tiles in all 8 adjacent directions.
    """

    # Tile grid offsets are half a tile offset as compared to original grid (positive direction),
    # thus +0,+0 is positioned to the bottom/right, and -1,-1 is positioned to the top/left
    lr_grid = tile_grids[(1, 0)]
    lr_cache = offset_cache[(1, 0)]
    _ensure_logits(tifs, lr_grid, tile_x - 1, tile_y + 0, flood_model, lr_cache)
    _ensure_logits(tifs, lr_grid, tile_x + 0, tile_y + 0, flood_model, lr_cache)

    ud_grid = tile_grids[(0, 1)]
    ud_cache = offset_cache[(0, 1)]
    _ensure_logits(tifs, ud_grid, tile_x + 0, tile_y - 1, flood_model, ud_cache)
    _ensure_logits(tifs, ud_grid, tile_x + 0, tile_y + 0, flood_model, ud_cache)

    di_grid = tile_grids[(1, 1)]
    di_cache = offset_cache[(1, 1)]
    _ensure_logits(tifs, di_grid, tile_x - 1, tile_y - 1, flood_model, di_cache)
    _ensure_logits(tifs, di_grid, tile_x + 0, tile_y - 1, flood_model, di_cache)
    _ensure_logits(tifs, di_grid, tile_x - 1, tile_y + 0, flood_model, di_cache)
    _ensure_logits(tifs, di_grid, tile_x + 0, tile_y + 0, flood_model, di_cache)

    return {
        "le": lr_cache[(tile_x - 1, tile_y + 0)],
        "ri": lr_cache[(tile_x + 0, tile_y + 0)],
        "up": ud_cache[(tile_x + 0, tile_y - 1)],
        "do": ud_cache[(tile_x + 0, tile_y + 0)],
        "tl": di_cache[(tile_x - 1, tile_y - 1)],
        "tr": di_cache[(tile_x + 0, tile_y - 1)],
        "bl": di_cache[(tile_x - 1, tile_y + 0)],
        "br": di_cache[(tile_x + 0, tile_y + 0)],
    }


@functools.lru_cache(1)
def _weight_matrices(h, w):
    """Creates a grid sized (h, w) of weights to apply to corners/edges of adjacent tiles"""
    # The -1 ensures that it's 1 at the edges and approaches 0 at the center
    spaces = np.array([(0, h // 2 - 0.5, h - 1), (0, w // 2 - 0.5, w - 1)])
    eval_coords = tuple(np.indices((h, w)))

    # Spaces describe the y and x independently (like for meshgrid)
    # Then lores is shaped like (len(yspace), len(xspace)), with lores at each coordinate
    # And eval_coords are the pixel coordinates as (Y, X) (like from meshgrid)
    def interp(lores):
        return scipy.interpolate.RegularGridInterpolator(spaces, lores)(eval_coords)

    # Create a grid of weight matrices, where the position in the grid indicates
    # where the weight matrix should be applied. e.g.
    # result[0, 0] is the weight matrix for top-left
    # result[1, 2] is the weight matrix for right, etc.
    result = np.array(
        [
            [
                interp([[1, 0, 0], [0, 0, 0], [0, 0, 0]]),
                interp([[0, 1, 0], [0, 0, 0], [0, 0, 0]]),
                interp([[0, 0, 1], [0, 0, 0], [0, 0, 0]]),
            ],
            [
                interp([[0, 0, 0], [1, 0, 0], [0, 0, 0]]),
                interp([[0, 0, 0], [0, 1, 0], [0, 0, 0]]),
                interp([[0, 0, 0], [0, 0, 1], [0, 0, 0]]),
            ],
            [
                interp([[0, 0, 0], [0, 0, 0], [1, 0, 0]]),
                interp([[0, 0, 0], [0, 0, 0], [0, 1, 0]]),
                interp([[0, 0, 0], [0, 0, 0], [0, 0, 1]]),
            ],
        ]
    )
    return result


def average_logits_towards_edges(logits, adjacent):

    c, h, w = logits.shape
    h2, w2 = h // 2, w // 2
    weights = _weight_matrices(h, w)
    slices = {
        "tl": (slice(0, h2), slice(0, w2)),
        "up": (slice(0, h2), slice(None)),
        "tr": (slice(0, h2), slice(w2, None)),
        "le": (slice(None), slice(0, w2)),
        "ri": (slice(None), slice(w2, None)),
        "bl": (slice(h2, None), slice(0, w2)),
        "do": (slice(h2, None), slice(None)),
        "br": (slice(h2, None), slice(w2, None)),
    }

    def f(tile, slc, islc, weight):
        if tile is None:
            return 0
        return tile[:, *islc] * weight[slc]

    out = np.zeros_like(logits)

    # Take the bottom-left corner of the top-right adjacent tile, and multiply by the weights
    # And similarly for the others. Complicated slightly by the fact they may not exist.
    out[:, *slices["tl"]] += f(adjacent["tl"], slices["tl"], slices["br"], weights[0, 0])
    out[:, *slices["up"]] += f(adjacent["up"], slices["up"], slices["do"], weights[0, 1])
    out[:, *slices["tr"]] += f(adjacent["tr"], slices["tr"], slices["bl"], weights[0, 2])
    out[:, *slices["le"]] += f(adjacent["le"], slices["le"], slices["ri"], weights[1, 0])
    out += logits * weights[1, 1]
    out[:, *slices["ri"]] += f(adjacent["ri"], slices["ri"], slices["le"], weights[1, 2])
    out[:, *slices["bl"]] += f(adjacent["bl"], slices["bl"], slices["tr"], weights[2, 0])
    out[:, *slices["do"]] += f(adjacent["do"], slices["do"], slices["up"], weights[2, 1])
    out[:, *slices["br"]] += f(adjacent["br"], slices["br"], slices["tl"], weights[2, 2])

    return out


def s1_preprocess_edge_heuristic(tensors, threshold=0.05):
    """Uses a heuristic to check if the tensor is not at the edge (i.e. False if at the edge)"""
    no_nan = np.all([np.isnan(t).sum() == 0 for t in tensors])
    # The tensors are at the edge if they have a significant proportion of 0s
    not_too_many_zeros = np.all([(t < 1e-5).sum() < (t.size * threshold) for t in tensors])
    return no_nan and not_too_many_zeros


def tile_flooded(stats, threshold=0.05):
    n_bg, n_pw, n_fl = stats
    return n_fl / (n_bg + n_pw + n_fl) > threshold


def sel_new_tiles_big_window(tile_x, tile_y, xsize, ysize, add=3):
    xlo = max(0, tile_x - add)
    xhi = min(xsize, tile_x + add)
    ylo = max(0, tile_y - add)
    yhi = min(ysize, tile_y + add)
    return list(itertools.product(range(xlo, xhi), range(ylo, yhi)))


def load_rivers(hydroatlas_path: Path, threshold: int = 100):
    river_path = hydroatlas_path / "filtered_rivers.gpkg"
    if river_path.exists():
        with warnings.catch_warnings(action="ignore"):
            return geopandas.read_file(
                river_path, engine="pyogrio", use_arrow=True, where=f"riv_tc_usu > {threshold}"
            )

    rivers = []
    for raw_path in hydroatlas_path.glob("**/RiverATLAS_v10_*.shp"):
        with warnings.catch_warnings(action="ignore"):
            r = geopandas.read_file(
                raw_path, engine="pyogrio", use_arrow=True, where=f"riv_tc_usu > {threshold}"
            )
        rivers.append(r)
    rivers_df = geopandas.GeoDataFrame(pd.concat(rivers, ignore_index=True), crs=rivers[0].crs)
    rivers_df.to_file(river_path, engine="pyogrio")
    return rivers_df


def run_snunet_once(
    imgs,
    geom: shapely.Geometry,
    model: torch.nn.Module,
    folder: Path,
    geom_in_px: bool = False,
    geom_crs: str = "EPSG:3857",
):
    inps = util.get_tiles_single(imgs, geom, geom_in_px)

    geom_4326 = util.convert_crs(geom, geom_crs, "EPSG:4326")
    try:
        dem_coarse = data_sources.get_dem(geom_4326, shp_crs="EPSG:4326", folder=folder)
    except data_sources.URLNotAvailable:
        return inps, None, None
    dem_fine = util.resample_xr(dem_coarse, geom_4326.bounds, inps[0].shape[2:])
    dem_np = dem_fine.band_data.values
    out = model(tuple(inps), dem=dem_np)[0].cpu().numpy()
    return inps, dem_np, out


def run_flood_vit_once(
    imgs, geom: shapely.Geometry, model: torch.nn.Module, geom_in_px: bool = False
):
    inps = util.get_tiles_single(imgs, geom, geom_in_px)
    out = model(tuple(inps))[0].cpu().numpy()
    return inps, None, out


def run_flood_vit_and_snunet_once(
    imgs,
    geom: shapely.Geometry,
    vit_model: torch.nn.Module,
    snunet_model: torch.nn.Module,
    folder: Path,
    geom_in_px: bool = False,
    geom_crs: str = "EPSG:3857",
):
    inps = util.get_tiles_single(imgs, geom, geom_in_px)

    geom_4326 = util.convert_crs(geom, geom_crs, "EPSG:4326")
    try:
        dem_coarse = data_sources.get_dem(geom_4326, shp_crs="EPSG:4326", folder=folder)
    except data_sources.URLNotAvailable:
        return inps, None, None
    dem_fine = util.resample_xr(dem_coarse, geom_4326.bounds, inps[0].shape[2:])
    dem_th = dem_fine.band_data.values
    vit_out = vit_model(tuple(inps))[0].cpu().numpy()
    snunet_out = snunet_model(tuple(inps[-2:]), dem=dem_th)[0].cpu().numpy()
    out = (vit_out + snunet_out) / 2
    return inps, dem_th, out


def run_flood_vit_batched(
    imgs, geoms: np.ndarray, model: torch.nn.Module, geoms_in_px: bool = False
):
    inps = util.get_tiles_batched(imgs, geoms, geoms_in_px)
    out = model(inps).cpu().numpy()
    return inps, None, out


def vit_decoder_runner():
    run_flood_model = lambda tifs, geom: run_flood_vit_once(tifs, geom, vit_model)
    return run_flood_model


def snunet_runner(crs, folder):
    run_flood_model = lambda tifs, geom: run_snunet_once(
        tifs[-2:], geom, snunet_model, folder, geom_crs=crs
    )
    return run_flood_model


def average_vit_snunet_runner(crs, folder):
    run_flood_model = lambda tifs, geom: run_flood_vit_and_snunet_once(
        tifs, geom, vit_model, snunet_model, folder, geom_crs=crs
    )
    return run_flood_model


vit_model = None
snunet_model = None


def model_runner(name: str, data_folder: Path):
    global vit_model
    global snunet_model

    if "vit" in name and vit_model is None:
        vit_model = torch.hub.load("Multihuntr/KuroSiwo", "vit_decoder", pretrained=True).cuda()
        if os.environ.get("CACHE_MODEL_OUTPUTS", "no")[0].lower() == "y":
            vit_model = util.np_cache(maxsize=3000)(vit_model)
    if "snunet" in name and snunet_model is None:
        snunet_model = torch.hub.load("Multihuntr/KuroSiwo", "snunet", pretrained=True).cuda()
        if os.environ.get("CACHE_MODEL_OUTPUTS", "no")[0].lower() == "y":
            snunet_model = util.np_cache(maxsize=3000)(snunet_model)

    if name == "vit":
        run_flood_model = vit_decoder_runner()
    elif name == "snunet":
        run_flood_model = snunet_runner("EPSG:4326", data_folder)
    elif name == "vit+snunet":
        run_flood_model = average_vit_snunet_runner("EPSG:4326", data_folder)
    else:
        raise NotImplementedError(f"Model named {name} not implemented")
    return run_flood_model


def postprocess_classes(in_fpath, out_fpath, **kwargs):
    with rasterio.open(in_fpath) as in_tif:
        floodmaps = in_tif.read()
        profile = in_tif.profile
        nodata = in_tif.nodata

    nan_mask = floodmaps == nodata

    floodmaps = _postprocess_classes(floodmaps[0], mask=(~nan_mask)[0], **kwargs)[None]

    floodmaps[nan_mask] = nodata
    with rasterio.open(out_fpath, "w", **profile) as out_tif:
        out_tif.write(floodmaps)


def _postprocess_classes(class_map, mask=None, size_threshold=50):
    # Smooth edges
    kernel = skimage.morphology.disk(radius=2)
    smoothed = skimage.filters.rank.majority(class_map, kernel, mask=mask)

    # Remove "small" holes in non-background classes
    h, w = smoothed.shape
    for cls_id in range(1, 3):
        for shp_np in skimage.measure.find_contours(smoothed == cls_id):
            if np.linalg.norm(shp_np[0] - shp_np[-1]) > 2:
                # This happens if the contour does not form a polygon
                # (e.g. cutting off a corner of the image)
                continue
            elif len(shp_np) <= 2:
                # This happens when there's a single pixel
                # Note: skimage contours places coordinates at half-pixels
                y, x = np.round(shp_np.mean(axis=0)).astype(np.int32)
                ylo, xlo = max(0, y - 2), max(0, x - 2)
                yhi, xhi = min(h, y + 3), min(w, x + 3)
                slices = (slice(ylo, yhi), slice(xlo, xhi))
                majority, _ = scipy.stats.mode(smoothed[slices], axis=None)
                smoothed[y, x] = majority
            else:
                shp = shapely.Polygon(shp_np)
                if shp.area < size_threshold:
                    ylo, xlo, yhi, xhi = util.rounded_bounds(shp.bounds)
                    ylo, xlo = max(0, ylo - 2), max(0, xlo - 2)
                    yhi, xhi = min(h, yhi + 3), min(w, xhi + 3)
                    slices = (slice(ylo, yhi), slice(xlo, xhi))
                    shp_mask = skimage.draw.polygon2mask(
                        (yhi - ylo, xhi - xlo), shp_np - (ylo, xlo)
                    )
                    majority, _ = scipy.stats.mode(smoothed[slices], axis=None)
                    smoothed[slices][shp_mask] = majority

    return smoothed


def postprocess_world_cover(data_folder, meta_fpath, basins_df, basins_geom):
    """
    Pastes world cover in the ocean tiles.
    Uses hydroatlas basins_df to determine "ocean tiles".
    """
    with open(meta_fpath) as f:
        meta = json.load(f)

    basin_idx = basins_df.HYBAS_ID.values.tolist().index(meta["HYBAS_ID_4"])
    basin_geoms = np.array(basins_df.geometry.values)

    # Create ocean shp - expand lvl4 basin, subtract all basins
    basin_geom = basin_geoms[basin_idx]
    # NOTE: Magic numbers coupled with util.majority_tile_mask_for_basin
    including_ocean = shapely.buffer(basin_geom, 0.04).simplify(0.01)
    ocean_shp = shapely.difference(including_ocean, basins_geom)

    # Check which tiles are in the ocean
    tile_fpath = meta_fpath.parent / meta["visit_tiles"]
    tiles = geopandas.read_file(tile_fpath, engine="pyogrio", use_arrow=True)
    tile_geoms = np.array(tiles.geometry.values)[:, None]
    ocean_mask = shapely.intersects(tile_geoms, ocean_shp)
    ocean_tiles = tiles[ocean_mask]

    floodmap_fpath = meta_fpath.parent / meta["floodmap"]
    with rasterio.open(floodmap_fpath, "r+") as tif:
        for i, tile in ocean_tiles.iterrows():
            # Get pixel bounds
            geom = tile.geometry
            window = util.shapely_bounds_to_rasterio_window(geom.bounds, tif.transform, align=True)
            (ylo, yhi), (xlo, xhi) = window
            data = tif.read(window=window)

            # Load worldcover
            cover = data_sources.get_world_cover(geom, tif.crs, data_folder)
            cover_res = util.resample_xr(cover, geom.bounds, ((yhi - ylo), (xhi - xlo)))

            # Apply permanent water class
            # Treat nans as water, as nans only exist outside worldcover bounds (the ocean)
            cover_np = cover_res.band_data.values
            nan_mask = np.isnan(cover_res.band_data.values)
            cover_np[nan_mask] = constants.WORLDCOVER_PW_CLASS
            # Paste over the generated floodmaps
            cover_np = cover_np.astype(np.uint8)
            permanent_water_mask = cover_np == constants.WORLDCOVER_PW_CLASS
            data[permanent_water_mask] = constants.KUROSIWO_PW_CLASS

            tif.write(data, window=window)
