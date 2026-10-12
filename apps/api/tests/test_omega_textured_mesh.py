"""Contracts for texture orientation, occlusion and mask-preserving surfaces."""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from omega_textured_mesh import mask_faces, rasterize, triangulate  # noqa: E402


def test_raster_integer_centres_texture_orientation_and_depth():
    screen = np.array([[0, 0], [7, 0], [0, 7]], float)
    z = np.full(3, 10.0)
    uv = (screen + 0.5) / 8
    texture = np.zeros((8, 8, 3), np.uint8)
    texture[:, :, 0] = np.arange(8)[None, :] * 30
    texture[:, :, 1] = np.arange(8)[:, None] * 30
    image = np.zeros((8, 8, 4), np.uint8)
    depth = np.full((8, 8), np.inf)
    faces = np.array([[0, 1, 2]])
    rasterize(screen, z, uv, faces, texture, image, depth)
    np.testing.assert_array_equal(image[2, 3], [90, 60, 0, 255])
    assert depth[2, 3] == pytest.approx(10)
    assert image[7, 7, 3] == 0
    rasterize(screen, z + 10, uv, faces, np.full_like(texture, 255), image, depth)
    np.testing.assert_array_equal(image[2, 3], [90, 60, 0, 255])


def test_perspective_correct_interpolation_is_inverse_z():
    screen = np.array([[0, 0], [6, 0], [0, 6]], float)
    z = np.array([5, 10, 20], float)
    uv = np.zeros((3, 2)) + 0.5
    image = np.zeros((8, 8, 4), np.uint8)
    depth = np.full((8, 8), np.inf)
    rasterize(
        screen,
        z,
        uv,
        np.array([[0, 1, 2]]),
        np.full((2, 2, 3), 100, np.uint8),
        image,
        depth,
    )
    assert depth[2, 2] == pytest.approx(1 / ((1 / 5 + 1 / 10 + 1 / 20) / 3))
    assert depth[2, 2] != pytest.approx(z.mean())


def test_mask_hole_inside_triangle_cannot_be_bridged():
    uv = np.array([[1, 1], [6, 1], [1, 6]], float)
    mask = np.ones((10, 10), bool)
    faces = np.array([[0, 1, 2]])
    assert mask_faces(uv, faces, mask)[0]
    mask[2, 2] = False
    assert not mask_faces(uv, faces, mask)[0]


def test_triangulation_keeps_input_positions_and_rejects_depth_discontinuity():
    uv = np.array([[3, 3], [4, 3], [3, 4], [4, 4]], float)
    xyz = np.column_stack((uv / 100, np.full(4, 10.0)))
    faces, _ = triangulate(uv, xyz, xyz[:, 2], np.ones((12, 12), bool))
    assert len(faces) == 2
    before = xyz.copy()
    xyz[-1, 2] = 30
    faces, _ = triangulate(uv, xyz, xyz[:, 2], np.ones((12, 12), bool))
    assert not any(3 in face for face in faces)
    np.testing.assert_array_equal(xyz[:3], before[:3])
