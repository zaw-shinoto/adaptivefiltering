from adaptivefiltering.paths import load_schema
from adaptivefiltering.utils import AdaptiveFilteringError

import geojson
import jsonschema
import ipyleaflet
import ipywidgets
import pdal
import json
import numpy as np


class Segment:
    def __init__(self, polygon, metadata={}):
        self.polygon = geojson.Polygon(polygon)
        self.metadata = metadata

    @property
    def metadata(self):
        return self._metadata

    @metadata.setter
    def metadata(self, _metadata):
        # Validate against our segment metadata schema
        schema = load_schema("segment_metadata.json")
        jsonschema.validate(instance=_metadata, schema=schema)
        self._metadata = _metadata

    @property
    def __geo_interface__(self):
        return {
            "type": "Feature",
            "geometry": self.polygon,
            "properties": self.metadata,
        }


class Segmentation(geojson.FeatureCollection):
    @classmethod
    def load(cls, filename):
        """Load segmentation from a filename

        :param filename:
            The filename to load from. Relative paths are interpreted
            w.r.t. the current working directory.
        :type filename: str
        """
        with open(filename, "r") as f:
            return Segmentation(geojson.load(f))

    def save(self, filename):
        """Save the segmentation to disk

        :param filename:
            The filename to save the segmentation to. Relative paths are interpreted
            w.r.t. the current working directory.
        :type filename: str
        """
        with open(filename, "w") as f:
            geojson.dump(self, f)

    @property
    def __geo_interface__(self):
        return {
            "type": "FeatureCollection",
            "features": self.features,
        }


class InteractiveMap:
    def __init__(self, dataset):
        """This class manages the interactive map on which one can choose the segmentation.

        :param dataset:
            The dataset from which the map should be displayed. This needs to be in a valid georeferenced format. Eg.: EPSG:4326
        :type dataset: Dataset

        """
        self.dataset = dataset
        self.coordinates_mean, self.polygon_boundary = self.get_boundry()

        self.m = ipyleaflet.Map(
            basemap=ipyleaflet.basemaps.Esri.WorldImagery,
            center=(self.coordinates_mean[0], self.coordinates_mean[1]),
            crs=ipyleaflet.projections.EPSG3857,
            scroll_wheel_zoom=True,
            max_zoom=20,
        )

        self.m.add_layer(self.polygon_boundary)

        # always add polygon draw tool and zoom slider
        self.add_zoom_slider()
        self.add_polygon_control()

        # get a color picker to put on the right side of the map
        self.color_pick = self.get_color_pick()
        # setup the grid with a list of widgets
        self.setup_grid([self.m, self.color_pick])

    def get_boundry(self):
        """takes the boundry coordinates of given dataset through the hexbin filter and returns them as a polygon

        :return:
        :param coordinates_mean:
            the approximate center of the polygon, this is where the map will be centered
        :type coordinates_mena: ndarray


        :param polygon_boundary:
            An ipyleaflet Polygon object which can be added to the map to mark the selected area.
        :type polygon_boundary: ipyleaflet Polygon

        """
        hexbin_json = [
            self.dataset.filename,
            {"type": "filters.reprojection", "out_srs": "EPSG:4326"},
            {"type": "filters.hexbin"},
        ]
        hexbin_pipeline = pdal.Pipeline(json.dumps(hexbin_json))
        hexbin_pipeline.execute()
        hexbin_geojson = json.loads(hexbin_pipeline.metadata)["metadata"][
            "filters.hexbin"
        ]["boundary_json"]

        # get the previous coordinate representation
        boundary_coordinates = hexbin_geojson["coordinates"][0]
        coordinates_mean = np.mean(boundary_coordinates, axis=0)
        polygon_boundary = ipyleaflet.Polygon(
            locations=boundary_coordinates, color="gray", opacity=0.9
        )
        return coordinates_mean, polygon_boundary

    def add_zoom_slider(self):
        """Adds the zoom slider to the interactive map.
        Also sets the default zoom
        """

        self.zoom_slider = ipywidgets.IntSlider(
            description="Zoom level:", min=0, max=20, value=16
        )
        ipywidgets.jslink((self.zoom_slider, "value"), (self.m, "zoom"))
        self.zoom_control1 = ipyleaflet.WidgetControl(
            widget=self.zoom_slider, position="topright"
        )
        self.m.add_control(self.zoom_control1)

    def add_polygon_control(self):
        """Adds the polygon draw control."""
        self.draw_control = ipyleaflet.DrawControl(
            layout=ipywidgets.Layout(width="auto", grid_area="main")
        )
        # deactivate polyline and circlemarker
        self.draw_control.polyline = {}
        self.draw_control.circlemarker = {}

        self.draw_control.polygon = {
            "shapeOptions": {
                "fillColor": "black",
                "color": "black",
                "fillOpacity": 0.1,
            },
            "drawError": {"color": "#dd253b", "message": "Oups!"},
            "allowIntersection": False,
        }

        self.m.add_control(self.draw_control)

    def get_color_pick(self):
        """Adds the color picker widget"""

        self.color_picker = ipywidgets.ColorPicker(
            description="Pick a color:",
            layout=ipywidgets.Layout(width="auto", grid_area="sidebar"),
        )
        self.color_picker.observe(self.update_draw_control_color)
        # self.m.add_control(self.color_control)
        return self.color_picker

    def setup_grid(self, objects):
        """Setup the grid layout to allow the color bar and
        more on the right side of the map.
        """
        self.grid = ipywidgets.GridBox(
            children=objects,
            layout=ipywidgets.Layout(
                width="100%",
                grid_template_columns="70% 30%",
                # grid_template_rows='90% 10%',
                grid_template_areas="""

            "main sidebar "

            """,
            ),
        )

    def update_draw_control_color(self, change, *args, **kwargs):
        """This is the automatic handler for detecting if a new color is selected."""

        self.m.remove_control(self.draw_control)
        self.draw_control.polygon = {
            "shapeOptions": {
                "fillColor": self.color_picker.get_interact_value(),
                "color": self.color_picker.get_interact_value(),
                "fillOpacity": 0.1,
            },
            "drawError": {"color": "#dd253b", "message": "Oups!"},
            "allowIntersection": False,
        }
        self.m.add_control(self.draw_control)

    def return_polygon(self):
        """Exports the current polygon list as a Segmentation object

        :return:
            :param segmentation:
                All current polygons in one segmentation object
            :type segmentation: Segmentation


        """

        segmentation = Segmentation(self.draw_control.data)
        return segmentation

    def load_polygon(self, segmentation):
        """imports a segmentation object onto the map.
        The function also checks for doubles.

        :param segmentation:
            A segmentation object which is to be loaded.
        :type segmentation: Segmentation


        """
        # imports the segmentation with a basic style to avoid issues after multiple savings and loadings.
        # This should preserve the json structure indefinitely as long as only our segmentations are loaded.

        # compare current data to new segmentation
        current_data = self.draw_control.data

        # filters only new polygons. to avoid double entrys. Ignores color and style, only checks for the geometry.
        new_polygons = [
            new_polygon
            for new_polygon in segmentation["features"]
            if not new_polygon["geometry"]
            in [data["geometry"] for data in current_data]
        ]
        # adds the new polygons to the current data
        new_data = current_data + new_polygons
        self.draw_control.data = new_data

    def show(self):
        """This functions returns the grid object and makes the map visible.
        :param grid:
            The grid object which holds the map and the right side interface
        :type grid: ipyleaflet.grid
        """
        return self.grid
