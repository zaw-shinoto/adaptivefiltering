from adaptivefiltering.dataset import DataSet
from adaptivefiltering.filter import Filter, PipelineMixin
from adaptivefiltering.paths import get_temporary_filename, load_schema, locate_file
from adaptivefiltering.segmentation import Segment, Segmentation
from adaptivefiltering.visualization import vis_hillshade, vis_mesh, vis_pointcloud
from adaptivefiltering.utils import AdaptiveFilteringError

import geodaisy.converters as convert
from osgeo import gdal
import json
import numpy as np
import os
import pdal
import pyrsistent
import tempfile


def execute_pdal_pipeline(dataset=None, config=None):
    """Execute a PDAL pipeline

    :param dataset:
        The :class:`~adaptivefiltering.DataSet` instance that this pipeline
        operates on. If :code:`None`, the pipeline will operate without inputs.
    :type dataset: :class:`~adaptivefiltering.DataSet`
    :param config:
        The configuration of the PDAL pipeline, according to the PDAL documentation.
    :type config: dict
    :return:
        A numpy array data structure containing the PDAL output.
    :rtype: numpy.array
    """
    # Make sure that a correct combination of arguments is given
    if config is None:
        raise ValueError("PDAL Pipeline configurations is required")

    # Undo stringification of the JSON to manipulate the pipeline
    if isinstance(config, str):
        config = json.loads(config)

    # Make sure that the JSON is a list of stages, even if just the
    # dictionary for a single stage was given
    if isinstance(config, dict):
        config = [config]

    # Construct the input array argument for the pipeline
    arrays = []
    if dataset is not None:
        arrays.append(dataset.data)

    # Define and execute the pipeline
    pipeline = pdal.Pipeline(json.dumps(config), arrays=arrays)
    _ = pipeline.execute()

    # We are currently only handling situations with one output array
    assert len(pipeline.arrays) == 1

    # Return the output array
    return pipeline.arrays[0]


class PDALFilter(Filter, identifier="pdal"):
    """A filter implementation based on PDAL"""

    def execute(self, dataset):
        dataset = PDALInMemoryDataSet.convert(dataset)
        config = pyrsistent.thaw(self.config)
        config.pop("_backend", None)
        return PDALInMemoryDataSet(
            data=execute_pdal_pipeline(dataset=dataset, config=config),
            provenance=dataset._provenance
            + [f"Applying PDAL filter with the following configuration:\n{config}"],
        )

    @classmethod
    def schema(cls):
        return load_schema("pdal.json")

    def as_pipeline(self):
        return PDALPipeline(filters=[self])


class PDALPipeline(
    PipelineMixin, PDALFilter, identifier="pdal_pipeline", backend=False
):
    def execute(self, dataset):
        dataset = PDALInMemoryDataSet.convert(dataset)
        pipeline_json = pyrsistent.thaw(self.config["filters"])
        for f in pipeline_json:
            f.pop("_backend", None)

        return PDALInMemoryDataSet(
            data=execute_pdal_pipeline(dataset=dataset, config=pipeline_json),
            provenance=dataset._provenance
            + [
                f"Applying PDAL pipeline with the following configuration:\n{pipeline_json}"
            ],
        )


class PDALInMemoryDataSet(DataSet):
    def __init__(self, data=None, provenance=[]):
        """An in-memory implementation of a Lidar data set that can used with PDAL

        :param data:
            The numpy representation of the data set. This argument is used by e.g. filters that
            already have the dataset in memory.
        :type data: numpy.array
        """
        # Store the given data and provenance array
        self.data = data
        super(PDALInMemoryDataSet, self).__init__(provenance=provenance)

    @classmethod
    def convert(cls, dataset):
        """Covert a dataset to a PDALInMemoryDataSet instance.

        This might involve file system based operations.

        :param dataset:
            The data set instance to convert.
        """
        # Conversion should be itempotent
        if isinstance(dataset, PDALInMemoryDataSet):
            return dataset

        # If dataset is of unknown type, we should first dump it to disk
        dataset = dataset.save(get_temporary_filename("las"))

        # Load the file from the given filename
        assert dataset.filename is not None

        filename = locate_file(dataset.filename)
        data = execute_pdal_pipeline(
            config={"type": "readers.las", "filename": filename}
        )
        return PDALInMemoryDataSet(
            data=data,
            provenance=dataset._provenance
            + [f"Loaded {data.shape[0]} points from {filename}"],
        )

    def save_mesh(
        self,
        filename,
        resolution=2.0,
    ):
        # if .tif is already in the filename it will be removed to avoid double file extension
        if os.path.splitext(filename)[1] == ".tif":
            filename = os.path.splitext(filename)[0]

        execute_pdal_pipeline(
            dataset=self,
            config={
                "filename": filename + ".tif",
                "gdaldriver": "GTiff",
                "output_type": "all",
                "resolution": resolution,
                "type": "writers.gdal",
            },
        )

        # Read the result
        self._geo_tif_data = gdal.Open(filename + ".tif", gdal.GA_ReadOnly)
        self._geo_tif_data_resolution = resolution

    def show_mesh(self, resolution=2.0):
        # check if a filename is given, if not make a temporary tif file to view data
        if self._geo_tif_data_resolution is not resolution:
            # Write a temporary file
            with tempfile.NamedTemporaryFile() as tmp_file:
                self.save_mesh(str(tmp_file.name), resolution=resolution)

        # use the number of x and y points to generate a grid.
        x = np.arange(0, self._geo_tif_data.RasterXSize)
        y = np.arange(0, self._geo_tif_data.RasterYSize)

        # multiplay x and y with the given resolution for comparable plot.
        x = x * self._geo_tif_data.GetGeoTransform()[1]
        y = y * self._geo_tif_data.GetGeoTransform()[1]

        # get height information from
        band = self._geo_tif_data.GetRasterBand(1)
        z = band.ReadAsArray()
        return vis_mesh(x, y, z)

    def show_points(self, threshold=750000):
        if len(self.data["X"]) >= threshold:
            error_text = "Too many Datapoints loaded for visualisation.{} points are loaded, but only {} allowed".format(
                len(self.data["X"]), threshold
            )
            raise ValueError(error_text)

        return vis_pointcloud(self.data["X"], self.data["Y"], self.data["Z"])

    def show_hillshade(self, resolution=2.0):
        # check if a filename is given, if not make a temporary tif file to view data
        if self._geo_tif_data_resolution is not resolution:
            # Write a temporary file
            with tempfile.NamedTemporaryFile() as tmp_file:
                self.save_mesh(str(tmp_file.name), resolution=resolution)

        band = self._geo_tif_data.GetRasterBand(1)

        return vis_hillshade(band.ReadAsArray())

    def save(self, filename, compress=False, overwrite=False):
        # Check if we would overwrite an input file
        if not overwrite and os.path.exists(filename):
            raise AdaptiveFilteringError(
                f"Would overwrite file '{filename}'. Set overwrite=True to proceed"
            )

        # Form the correct configuration string for compression
        compress = "laszip" if compress else "none"

        # Exectute writer pipeline
        execute_pdal_pipeline(
            dataset=self,
            config={
                "filename": filename,
                "type": "writers.las",
                "compression": compress,
            },
        )

        # Wrap the result in a DataSet instance
        return DataSet(filename=filename)

    def restrict(self, segmentation):
        # If a single Segment is given, we convert it to a segmentation
        if isinstance(segmentation, Segment):
            segmentation = Segmentation([segmentation])

        # Construct an array of WKT Polygons for the clipping
        polygons = [convert.geojson_to_wkt(s.polygon) for s in segmentation["features"]]

        from adaptivefiltering.pdal import execute_pdal_pipeline

        # Apply the cropping filter with all polygons
        newdata = execute_pdal_pipeline(
            dataset=self, config={"type": "filters.crop", "polygon": polygons}
        )

        return PDALInMemoryDataSet(
            data=newdata,
            provenance=self._provenance
            + [f"Cropping data to only include polygons defined by:\n{str(polygons)}"],
        )
