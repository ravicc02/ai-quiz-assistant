from __future__ import annotations

import math


_X_PI = math.pi * 3000.0 / 180.0
_PI = math.pi
_A = 6378245.0
_EE = 0.00669342162296594323


def out_of_china(longitude, latitude):
    return not (72.004 <= longitude <= 137.8347 and 0.8293 <= latitude <= 55.8271)


def _transform_latitude(longitude, latitude):
    value = (
        -100.0
        + 2.0 * longitude
        + 3.0 * latitude
        + 0.2 * latitude * latitude
        + 0.1 * longitude * latitude
        + 0.2 * math.sqrt(abs(longitude))
    )
    value += (
        20.0 * math.sin(6.0 * longitude * _PI)
        + 20.0 * math.sin(2.0 * longitude * _PI)
    ) * 2.0 / 3.0
    value += (
        20.0 * math.sin(latitude * _PI)
        + 40.0 * math.sin(latitude / 3.0 * _PI)
    ) * 2.0 / 3.0
    value += (
        160.0 * math.sin(latitude / 12.0 * _PI)
        + 320.0 * math.sin(latitude * _PI / 30.0)
    ) * 2.0 / 3.0
    return value


def _transform_longitude(longitude, latitude):
    value = (
        300.0
        + longitude
        + 2.0 * latitude
        + 0.1 * longitude * longitude
        + 0.1 * longitude * latitude
        + 0.1 * math.sqrt(abs(longitude))
    )
    value += (
        20.0 * math.sin(6.0 * longitude * _PI)
        + 20.0 * math.sin(2.0 * longitude * _PI)
    ) * 2.0 / 3.0
    value += (
        20.0 * math.sin(longitude * _PI)
        + 40.0 * math.sin(longitude / 3.0 * _PI)
    ) * 2.0 / 3.0
    value += (
        150.0 * math.sin(longitude / 12.0 * _PI)
        + 300.0 * math.sin(longitude / 30.0 * _PI)
    ) * 2.0 / 3.0
    return value


def wgs84_to_gcj02(longitude, latitude):
    longitude = float(longitude)
    latitude = float(latitude)
    if out_of_china(longitude, latitude):
        return longitude, latitude
    latitude_delta = _transform_latitude(longitude - 105.0, latitude - 35.0)
    longitude_delta = _transform_longitude(longitude - 105.0, latitude - 35.0)
    radial_latitude = latitude / 180.0 * _PI
    magic = math.sin(radial_latitude)
    magic = 1 - _EE * magic * magic
    sqrt_magic = math.sqrt(magic)
    latitude_delta = (
        latitude_delta * 180.0
        / ((_A * (1 - _EE)) / (magic * sqrt_magic) * _PI)
    )
    longitude_delta = (
        longitude_delta * 180.0
        / (_A / sqrt_magic * math.cos(radial_latitude) * _PI)
    )
    return longitude + longitude_delta, latitude + latitude_delta


def gcj02_to_bd09(longitude, latitude):
    longitude = float(longitude)
    latitude = float(latitude)
    radius = math.sqrt(longitude * longitude + latitude * latitude) + (
        0.00002 * math.sin(latitude * _X_PI)
    )
    angle = math.atan2(latitude, longitude) + 0.000003 * math.cos(longitude * _X_PI)
    return (
        radius * math.cos(angle) + 0.0065,
        radius * math.sin(angle) + 0.006,
    )


def wgs84_to_bd09(longitude, latitude):
    return gcj02_to_bd09(*wgs84_to_gcj02(longitude, latitude))
