import base64
import colorsys
import copy
import json
import os
import shutil
from itertools import groupby
from typing import Dict, Tuple, TypeVar

from qgis.core import QgsExpression

from ...util.network_helper import http_get
from .data import qgis_functions
from .xml_helper import create_style_file, escape_xml

StrOrDict = TypeVar("StrOrDict", str, dict)


def register_qgis_expressions():
    QgsExpression.registerFunction(qgis_functions.get_zoom_for_scale)
    QgsExpression.registerFunction(qgis_functions.if_not_exists)
    QgsExpression.registerFunction(qgis_functions.interpolate_exp)


def get_background_color(text):
    js = json.loads(text)
    layers = js["layers"]
    bg_color = None
    for l in layers:
        if l["type"] == "background":
            if "paint" in l:
                paint = l["paint"]
                if "background-color" in paint:
                    bg_color = paint["background-color"]
                    if bg_color:
                        bg_color = parse_color(bg_color)
            break
    if bg_color and not bg_color.startswith("#"):
        colors = list(map(lambda v: int(v), bg_color.split(",")))
        bg_color = "#{0:02x}{1:02x}{2:02x}".format(colors[0], colors[1], colors[2])
    return bg_color


def generate_styles(style_json: StrOrDict, output_directory: str) -> None:
    """
     * Creates and exports the styles
    :param style_json:
    :param output_directory:
    :return:
    """
    try:
        style_json = json.loads(style_json)
    except:
        pass
    styles = process(style_json)
    write_styles(styles_by_target_layer=styles, output_directory=output_directory)
    create_icons(style=style_json, output_directory=output_directory)


def _apply_source_layer(layer, all_layers):
    """
     * Recursivly applies all properties except 'paint' from the layer specified by 'ref'.
     * Layers can reference each other with the 'ref' property, in which case the properties are located on the
       referenced layer.
    :param layer:
    :param all_layers:
    :return:
    """

    ref = _get_value_safe(layer, "ref")
    if ref:
        matching_layers = list(filter(lambda l: _get_value_safe(l, "id") == ref, all_layers))
        if matching_layers:
            target_layer = matching_layers[0]
            for prop in target_layer:
                if prop != "paint":
                    layer[prop] = target_layer[prop]
            _apply_source_layer(target_layer, all_layers)


def process(style_json):
    """
     * Creates the style definitions and returns them mapped by filename
    :param style_json:
    :return:
    """
    if not isinstance(style_json, dict):
        style_json = json.loads(style_json)

    layers = style_json["layers"]
    styles_by_file_name = {}
    for l in layers:
        if "ref" in l:
            _apply_source_layer(l, layers)
        if "source-layer" not in l:
            continue
        source_layer = l["source-layer"]
        layer_type = l["type"]
        if source_layer:
            if layer_type == "fill":
                geo_type_name = ".polygon"
            elif layer_type == "line":
                geo_type_name = ".linestring"
            elif layer_type == "symbol":
                geo_type_name = ""
            else:
                continue

            file_name = "{}{}.qml".format(source_layer, geo_type_name)
            if file_name not in styles_by_file_name:
                styles_by_file_name[file_name] = {"file_name": file_name, "type": layer_type, "styles": []}
            qgis_styles = get_styles(l)
            filter_expr = None
            if "filter" in l:
                filter_expr = get_qgis_rule(l["filter"])
            for s in qgis_styles:
                s["rule"] = filter_expr
            styles_by_file_name[file_name]["styles"].extend(qgis_styles)

    for layer_name in styles_by_file_name:
        styles = styles_by_file_name[layer_name]["styles"]
        for index, style in enumerate(styles):
            rule = style["rule"]
            name = style["name"]
            zoom = style["zoom_level"]
            styles_with_same_target = list(
                filter(
                    lambda st: st["name"] != name and st["rule"] == rule and zoom and st["zoom_level"] <= zoom,
                    styles[:index],
                )
            )
            groups_by_name = list(groupby(styles_with_same_target, key=lambda st: st["name"]))
            style["rendering_pass"] = len(groups_by_name)

    _add_default_transparency_styles(styles_by_file_name)
    return styles_by_file_name


def create_icons(style, output_directory):
    """
    Loads the sprites defined by sprites.json and sprites.data and extracts the specific items by creating
    svg icons that clip the defined region (defined in sprites.json) from the sprites.png so that only one icons
    remains.
    :param style:
    :param output_directory:
    :return:
    """

    image_data, image_definition_data = _load_sprite_data(style)
    if image_data and image_definition_data:
        _create_icons(image_data, image_definition_data, output_directory)


def _create_icons(image_base64, image_definition_data, output_directory):
    icons_directory = os.path.join(output_directory, "icons")
    if not os.path.isdir(icons_directory):
        os.makedirs(icons_directory)

    with open(os.path.join(icons_directory, "sprite.json"), "w") as f:
        f.write(json.dumps(image_definition_data))

    src_icon_path = os.path.join(os.path.dirname(__file__), "data", "icons", "empty.svg")
    shutil.copy2(src_icon_path, icons_directory)

    template_path = os.path.join(os.path.dirname(__file__), "data", "svg_template.svg")
    assert os.path.isfile(template_path)
    with open(template_path, "r") as f:
        template_data = f.read()
    for name in image_definition_data:
        img_def = image_definition_data[name]
        file_name = "{}.svg".format(name)
        svg_data = template_data.format(
            width=img_def["width"], height=img_def["height"], x=img_def["x"], y=img_def["y"], base64_data=image_base64
        )
        target_file = os.path.join(icons_directory, file_name)
        with open(target_file, "w") as f:
            f.write(svg_data)


def _load_sprite_data(style: dict) -> Tuple:
    image_data = None
    image_definition_data = None
    if "sprite" in style:
        image_url = "{}.png".format(style["sprite"])
        image_definitions_url = "{}.json".format(style["sprite"])
        if not image_url.startswith("http") or not image_definitions_url.startswith("http"):
            return None, None

        _, image_data = http_get(image_url)
        _, image_definition_data = http_get(image_definitions_url)
        if not image_data:
            raise "No image found at: {}".format(image_url)
        else:
            image_data = base64.b64encode(image_data)

        if not image_definition_data:
            raise "No image definitions found at: {}".format(image_definition_data)
        else:
            image_definition_data = json.loads(str(image_definition_data))
    return image_data, image_definition_data


def _add_default_transparency_styles(style: dict):
    for t in ["point", "linestring", "polygon"]:
        file_name = "transparent.{}.qml".format(t)
        style[file_name] = {"styles": [], "file_name": file_name, "layer-transparency": 100, "type": None}


def write_styles(styles_by_target_layer: Dict[str, dict], output_directory: str) -> None:
    """
    Creates the qml files that can be applied to qgis layers.
    :param styles_by_target_layer:
    :param output_directory:
    :return:
    """

    if os.path.isdir(output_directory):
        for the_file in os.listdir(output_directory):
            file_path = os.path.join(output_directory, the_file)
            if os.path.isfile(file_path) and file_path.lower().endswith(".qml"):
                os.unlink(file_path)

    for layer_name in styles_by_target_layer:
        style = styles_by_target_layer[layer_name]
        create_style_file(output_directory=output_directory, layer_style=style)


_comparision_operators = {"match": "=", "==": "=", "<=": "<=", ">=": ">=", "<": "<", ">": ">", "!=": "!="}

_combining_operators = {"all": "and", "any": "or", "none": "and not"}

_membership_operators = {"in": "in", "!in": "not in"}

_existential_operators = {"has": "is not null", "!has": "is null"}

"""
 * the upper bound map scales by zoom level.
 * E.g. a style for zoom level 14 shall be applied for map scales <= 50'000
 * If the are more zoom levels applied, they need to be ascending.
 * E.g. a second style will be applied for zoom level 15, that is map scale <= 25'000, the first style
   for zoom level 14 will no longer be active.
"""
upper_bound_map_scales_by_zoom_level = {
    0: 1000000000,
    1: 1000000000,
    2: 500000000,
    3: 200000000,
    4: 50000000,
    5: 25000000,
    6: 12500000,
    7: 6500000,
    8: 3000000,
    9: 1500000,
    10: 750000,
    11: 400000,
    12: 200000,
    13: 100000,
    14: 50000,
    15: 25000,
    16: 12500,
    17: 5000,
    18: 2500,
    19: 1500,
    20: 750,
    21: 500,
    22: 250,
    23: 100,
    24: 0,
}


def get_styles(layer):
    layer_id = layer["id"]
    if "type" not in layer:
        raise RuntimeError("'type' not set on layer")

    layer_type = layer["type"]

    base_style = {"zoom_level": None, "min_scale_denom": None, "max_scale_denom": None}

    if layer_id:
        base_style["name"] = layer_id

    resulting_styles = []
    values_by_zoom = {}

    all_values = []
    if layer_type == "fill":
        all_values.extend(
            get_properties_values_for_zoom(layer, "paint/fill-color", is_color=True, default="rgba(0,0,0,0)")
        )
        all_values.extend(get_properties_values_for_zoom(layer, "paint/fill-outline-color", is_color=True))
        all_values.extend(get_properties_values_for_zoom(layer, "paint/fill-translate"))
        all_values.extend(get_properties_values_for_zoom(layer, "paint/fill-opacity"))
        all_values.extend(get_properties_values_for_zoom(layer, "paint/fill-pattern", is_expression=True))
    elif layer_type == "line":
        all_values.extend(get_properties_values_for_zoom(layer, "layout/line-join"))
        all_values.extend(get_properties_values_for_zoom(layer, "layout/line-cap"))
        all_values.extend(get_properties_values_for_zoom(layer, "paint/line-width", default=0, can_interpolate=True))
        all_values.extend(
            get_properties_values_for_zoom(layer, "paint/line-color", is_color=True, default="rgba(0,0,0,0)")
        )
        all_values.extend(get_properties_values_for_zoom(layer, "paint/line-opacity"))
        all_values.extend(get_properties_values_for_zoom(layer, "paint/line-dasharray"))
    elif layer_type == "symbol":
        all_values.extend(get_properties_values_for_zoom(layer, "layout/icon-image", is_expression=True))
        all_values.extend(get_properties_values_for_zoom(layer, "layout/text-font"))
        all_values.extend(get_properties_values_for_zoom(layer, "layout/text-transform"))
        all_values.extend(get_properties_values_for_zoom(layer, "layout/text-size", can_interpolate=True))
        all_values.extend(get_properties_values_for_zoom(layer, "layout/text-field", is_expression=True, take=1))
        all_values.extend(get_properties_values_for_zoom(layer, "layout/text-max-width"))
        all_values.extend(get_properties_values_for_zoom(layer, "paint/text-color", is_color=True))
        all_values.extend(get_properties_values_for_zoom(layer, "paint/text-halo-width", can_interpolate=True))
        all_values.extend(get_properties_values_for_zoom(layer, "paint/text-halo-color", is_color=True))

    for v in all_values:
        zoom = v["zoom_level"]
        if zoom is None:
            base_style[v["name"]] = v["value"]
        else:
            if zoom not in values_by_zoom:
                values_by_zoom[zoom] = []
            values_by_zoom[zoom].append(v)

    if "minzoom" in layer:
        minzoom = int(layer["minzoom"])
        if minzoom not in values_by_zoom:
            values_by_zoom[minzoom] = []
    if "maxzoom" in layer:
        maxzoom = int(layer["maxzoom"])
        if maxzoom not in values_by_zoom:
            values_by_zoom[maxzoom] = []

    if not values_by_zoom:
        resulting_styles.append(base_style)
    else:
        clone = base_style
        for zoom in sorted(values_by_zoom.keys()):
            values = values_by_zoom[zoom]
            clone = copy.deepcopy(clone)
            clone["zoom_level"] = zoom
            for v in values:
                clone[v["name"]] = v["value"]
            resulting_styles.append(clone)
        styles_backwards = list(reversed(resulting_styles))
        for index, s in enumerate(styles_backwards):
            if index < len(resulting_styles) - 1:
                next_style = styles_backwards[index + 1]
                for k in s:
                    if k not in next_style:
                        next_style[k] = s[k]

    resulting_styles = sorted(resulting_styles, key=lambda st: st["zoom_level"])
    _apply_scale_range(resulting_styles)

    return resulting_styles


def _parse_expr(expr, take=None):
    """
     * Creates a QGIS expression (e.g. '{maki}-11'  becomes '"maki"'+'11'
    :param expr:
    :param take: The nr of fields to take. All if value is None.
                 E.g.: "{name:latin}\n{name:nonlatin}" consists of two fields
    :return:
    """
    if isinstance(expr, list):
        op = expr[0]
        data_expression_operators = ["get", "has", "id", "geometry-type", "properties", "feature-state"]
        is_data_expression = True if op in data_expression_operators else False
        if not is_data_expression:
            return ""
        else:
            if op == "get":
                assert len(expr) == 2
                return _parse_expr("{}".format(expr[1]))
            else:
                raise RuntimeError("Data expression operator not implemented: ", op)

    fields = _get_qgis_fields(expr)[:take]

    #issue starts here and can be checked for and fixed
    #when the http_get returns byte information, that 'b' sometimes sticks to icon name
    #simply check and if it is there, remove
    b_string = "'b'"
    for s, item in enumerate(fields):
        if b_string in item:
            new_item = item[2:-1]
            fields[s] = new_item

    result = "+".join(fields)
    return escape_xml(result)


def _map_value_to_qgis_expr(val):
    """
    Wraps the value either in double quotes if it's an expression or in single quotes if it's a string/value
    :param val:
    :return:
    """

    if val["is_expr"]:
        return '"{}"'.format(val["text"].encode("utf-8"))
    else:
        return "'{}'".format(val["text"].encode("utf-8"))


def _get_qgis_fields(expr):
    if isinstance(expr, list):
        # todo: what happens here?
        raise RuntimeError("invalid: ", expr)

    values = []
    val = None
    is_expr = False
    new_is_expr = False
    for s in expr:
        if not is_expr:
            new_is_expr = s == "{"
        elif s == "}":
            new_is_expr = False
        has_changed = new_is_expr != is_expr
        is_expr = new_is_expr
        if has_changed:
            if val:
                values.append(val)
            val = None
        if has_changed:
            continue
        if val is None:
            val = {"text": "", "is_expr": is_expr}
        if isinstance(s, list):
            raise RuntimeError("Unknown s: ", expr)
        val["text"] += s
    if val:
        values.append(val)
    mapped = list(map(_map_value_to_qgis_expr, values))
    return mapped


def _get_field_expr(index, field):
    if index > 0:
        return "if({field} is null, '', '\\n' + {field})".format(field=field.encode("utf-8"))
    else:
        return "if({field} is null, '', {field})".format(field=field.encode("utf-8"))


def parse_color(color):
    if isinstance(color, list):
        if color[0] != "match":
            # raise RuntimeError("Unknown color: ", color)
            return "255,0,0,0"
        return _get_match_expr(color)
    elif color.startswith("#"):
        color = color.replace("#", "")
        if len(color) == 3:
            color = "".join(list(map(lambda c: c + c, color)))
        elif len(color) == 6:
            return "#" + color
        return ",".join(list(map(lambda v: str(int(v)), bytearray.fromhex(color))))
    else:
        return _get_color_string(color)


def _get_color_string(color):
    color = color.lower()
    has_alpha = color.startswith("hsla(") or color.startswith("rgba(")
    is_hsl = color.startswith("hsl")
    colors = (
        color.replace("hsla(", "")
        .replace("hsl(", "")
        .replace("rgba(", "")
        .replace("rgb(", "")
        .replace(")", "")
        .replace(" ", "")
        .replace("%", "")
        .split(",")
    )
    colors = list(map(lambda c: float(c), colors))
    a = 1
    if has_alpha:
        a = colors[3]
    if is_hsl:
        h = colors[0] / 360.0
        s = colors[1] / 100.0
        l_value = colors[2] / 100.0
        r, g, b = colorsys.hls_to_rgb(h, l_value, s)
        return ",".join(list(map(lambda c: str(int(round(255.0 * c))), [r, g, b, a])))
    else:
        r = colors[0]
        g = colors[1]
        b = colors[2]
        a = round(255.0 * a)
        return ",".join(list(map(lambda c: str(int(c)), [r, g, b, a])))


def _apply_scale_range(styles):
    for index, s in enumerate(styles):
        if s["zoom_level"] is None:
            continue

        max_scale_denom = upper_bound_map_scales_by_zoom_level[s["zoom_level"]]
        if index == len(styles) - 1:
            min_scale_denom = 1
        else:
            zoom_of_next = styles[index + 1]["zoom_level"]
            min_scale_denom = upper_bound_map_scales_by_zoom_level[zoom_of_next]
        s["min_scale_denom"] = min_scale_denom
        s["max_scale_denom"] = max_scale_denom


def _get_property_value(obj, property_path):
    parts = property_path.split("/")
    value = obj
    for p in parts:
        value = _get_value_safe(value, p)
    property_name = parts[-1]
    return value, property_name


def get_properties_values_for_zoom(
    paint, property_path, is_color=False, is_expression=False, can_interpolate=False, default=None, take=None
):
    """
    Returns a list of properties (defined by name, zoom_level, value and is_qgis_expr)
    If stops are defined, multiple properties will be in the list. One for each zoom-level defined by the stops.
    If no stops are defined, only a single property (for zoom_level None) will be added to the list.
    :param paint: The paint object (according to the Mapbox GL Style spec)
    :param property_path: The path to the property, delimited by a forward-slash. E.g. 'paint/fill-color'
    :param is_color: True if the property defines a color, like fill-color, line-color, etc.
    :param is_expression: True if the value represents a qgis expression
    :param can_interpolate:
    :param default:
    :param take:
    :return:
    """

    if (is_color or is_expression) and can_interpolate:
        raise RuntimeError("Colors and expressions cannot be interpolated")

    value, property_name = _get_property_value(obj=paint, property_path=property_path)

    stops = None
    if value is not None:
        stops = _get_value_safe(value, "stops")
    else:
        value = default
    properties = []
    if stops:
        base = _get_value_safe(value, "base")
        if not base:
            base = 1
        stops_to_iterate = stops
        if can_interpolate:
            stops_to_iterate = stops[:-1]

        for index, stop in enumerate(stops_to_iterate):
            is_qgis_expr = is_expression
            lower_zoom = stop[0]
            value = stop[1]
            if is_color:
                value = parse_color(value)
            if is_expression:
                value = _parse_expr(value, take=take)
            if can_interpolate:
                is_qgis_expr = True
                next_stop = stops[index + 1]
                upper_zoom = next_stop[0]
                second_value = next_stop[1]
                max_scale = upper_bound_map_scales_by_zoom_level[int(lower_zoom)]
                min_scale = upper_bound_map_scales_by_zoom_level[int(upper_zoom)]
                value = (
                    "interpolate_exp(get_zoom_for_scale(@map_scale), {base}, {min_zoom}, {max_zoom}, "
                    "{first_value}, {second_value})".format(
                        min_zoom=int(lower_zoom),
                        max_zoom=int(upper_zoom),
                        base=base,
                        min_scale=min_scale,
                        max_scale=max_scale,
                        first_value=value,
                        second_value=second_value,
                    )
                )
            properties.append(
                {"name": property_name, "zoom_level": int(lower_zoom), "value": value, "is_qgis_expr": is_qgis_expr}
            )
    elif value is not None:
        if is_color:
            value = parse_color(value)
        if is_expression:
            value = _parse_expr(value, take=take)
        properties.append({"name": property_name, "zoom_level": None, "value": value, "is_qgis_expr": is_expression})
    return properties


def _get_rules_for_stops(stops):
    rules = []
    for s in stops:
        zoom_level = int(s[0])
        scale = upper_bound_map_scales_by_zoom_level[zoom_level]
        rule = get_qgis_rule(["<=", "@map_scale", scale])
        rules.append({"rule": rule, "value": s[1]})
    return rules


def _get_value_safe(value, path):
    result = None
    if value is not None and isinstance(value, dict) and path and path in value:
        result = value[path]
    return result


def get_qgis_rule(mb_filter, escape_result=True, depth=0):
    op = mb_filter[0]
    if op in _comparision_operators:
        result = _get_comparision_expr(mb_filter)
    elif op in _combining_operators:
        is_none = op == "none"
        all_exprs = map(lambda f: get_qgis_rule(f, escape_result=False, depth=depth + 1), mb_filter[1:])
        all_exprs = list(filter(lambda e: e is not None, all_exprs))
        comb_op = _combining_operators[op]
        if comb_op == "and" and len(all_exprs) > 1:
            all_exprs = list(map(lambda e: "({})".format(e), all_exprs))
        full_expr = " {} ".format(comb_op).join(all_exprs)

        if is_none:
            full_expr = "not {}".format(full_expr)
        result = full_expr
    elif op in _membership_operators:
        result = _get_membership_expr(mb_filter)
    elif op in _existential_operators:
        result = _get_existential_expr(mb_filter)
    else:
        raise NotImplementedError("Not Implemented Operator: '{}', Filter: {}".format(op, mb_filter))

    if result:
        # if depth > 0:
        #     result = "({})".format(result)
        if escape_result:
            result = escape_xml(result)
    return result


class If(object):
    def __init__(self, comparision, if_value, else_if=None):
        self.comparision = comparision
        self.if_value = if_value
        self.else_if = else_if

    def dumps(self):
        else_dump = '""'
        if self.else_if:
            if isinstance(self.else_if, str):
                else_dump = "'{}'".format(self.else_if)
            else:
                assert isinstance(self.else_if, If)
                else_dump = self.else_if.dumps()

        return "if ({}, {}, {})".format(self.comparision, "'{}'".format(self.if_value), else_dump)

    def __repr__(self):
        return self.dumps()


def _get_match_expr(match):
    """
    Implements the expression match
    See: https://docs.mapbox.com/mapbox-gl-js/style-spec/#expressions-match
    :param match:
    :return:
    """

    assert isinstance(match, list)
    assert len(match) >= 4 and divmod(len(match), 2)[1] == 1  # the last value is the fallback
    assert match[0] in _comparision_operators

    labels = match[2:-1:2]
    all_outputs = list(map(lambda c: parse_color(c), match[3::2]))
    fallback = parse_color(match[-1])
    labels_with_output = zip(labels, all_outputs)
    op = match[0]
    attr = match[1][1]
    root_if = None
    current_if = None

    for label, output in labels_with_output:
        expr = _get_comparision_expr([op, attr, label])
        new_if = If(expr, output)
        if not root_if:
            root_if = new_if
            current_if = root_if

        if current_if != new_if:
            current_if.else_if = new_if
            current_if = new_if
    current_if.else_if = fallback

    return root_if.dumps()


def _get_comparision_expr(mb_filter):
    assert mb_filter[0] in _comparision_operators
    assert len(mb_filter) == 3
    op = _comparision_operators[mb_filter[0]]
    attr = mb_filter[1]
    value = mb_filter[2]
    if attr == "$type":
        return None
    attr_in_quotes = not attr.startswith("@")
    if attr_in_quotes:
        attr = '"{}"'.format(attr)
    null_allowed = op == "!="
    if null_allowed:
        null = "{attr} is null or {attr}"
    else:
        null = "{attr} is not null and {attr}"
    attr = null.format(attr=attr)
    return "{attr} {op} '{value}'".format(attr=attr, op=op, value=value)


def _get_membership_expr(mb_filter):
    assert mb_filter[0] in _membership_operators
    assert len(mb_filter) >= 3
    what = '"{}"'.format(mb_filter[1])
    op = _membership_operators[mb_filter[0]]
    collection = "({})".format(", ".join(list(map(lambda e: "'{}'".format(e), mb_filter[2:]))))
    is_not = mb_filter[0].startswith("!")
    if is_not:
        null_str = "is null or"
    else:
        null_str = "is not null and"
    return "({what} {null} {what} {op} {coll})".format(what=what, op=op, coll=collection, null=null_str)


def _get_existential_expr(mb_filter):
    assert mb_filter[0] in _existential_operators
    assert len(mb_filter) == 2
    op = _existential_operators[mb_filter[0]]
    key = mb_filter[1]
    return "attribute($currentfeature, '{key}') {op}".format(key=key, op=op)
