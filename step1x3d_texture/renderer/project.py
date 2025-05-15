import torch
import pytorch3d


from pytorch3d.io import load_objs_as_meshes, load_obj, save_obj, IO

from pytorch3d.structures import Meshes
from pytorch3d.renderer import (
    look_at_view_transform,
    FoVPerspectiveCameras,
    FoVOrthographicCameras,
    AmbientLights,
    PointLights,
    DirectionalLights,
    Materials,
    RasterizationSettings,
    MeshRenderer,
    MeshRasterizer,
    TexturesUV,
)

from .geometry import HardGeometryShader
from .shader import HardNChannelFlatShader
from .voronoi import voronoi_solve
import torch.nn.functional as F
import open3d as o3d
import pdb
import kaolin as kal
import numpy as np


import torch
from pytorch3d.renderer.cameras import FoVOrthographicCameras
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union
from pytorch3d.common.datatypes import Device
import math
import torch.nn.functional as F
from trimesh import Trimesh
from pytorch3d.structures import Meshes
import os

LIST_TYPE = Union[list, np.ndarray, torch.Tensor]

_R = torch.eye(3)[None]  # (1, 3, 3)
_T = torch.zeros(1, 3)  # (1, 3)
_BatchFloatType = Union[float, Sequence[float], torch.Tensor]


class CustomOrthographicCameras(FoVOrthographicCameras):
    def compute_projection_matrix(
        self, znear, zfar, max_x, min_x, max_y, min_y, scale_xyz
    ) -> torch.Tensor:
        """
        自定义正交投影矩阵计算，继承并修改深度通道参数
        参数维度说明:
        - znear/zfar: (N,)
        - max_x/min_x: (N,)
        - max_y/min_y: (N,)
        - scale_xyz: (N, 3)
        """
        K = torch.zeros((self._N, 4, 4), dtype=torch.float32, device=self.device)

        ones = torch.ones((self._N), dtype=torch.float32, device=self.device)
        # NOTE: OpenGL flips handedness of coordinate system between camera
        # space and NDC space so z sign is -ve. In PyTorch3D we maintain a
        # right handed coordinate system throughout.
        z_sign = +1.0

        K[:, 0, 0] = (2.0 / (max_x - min_x)) * scale_xyz[:, 0]
        K[:, 1, 1] = (2.0 / (max_y - min_y)) * scale_xyz[:, 1]
        K[:, 0, 3] = -(max_x + min_x) / (max_x - min_x)
        K[:, 1, 3] = -(max_y + min_y) / (max_y - min_y)
        K[:, 3, 3] = ones

        # NOTE: This maps the z coordinate to the range [0, 1] and replaces the
        # the OpenGL z normalization to [-1, 1]
        K[:, 2, 2] = -2 * (1.0 / (zfar - znear)) * scale_xyz[:, 2]
        K[:, 2, 3] = -(znear + zfar) / (zfar - znear)

        return K

    def __init__(
        self,
        znear: _BatchFloatType = 1.0,
        zfar: _BatchFloatType = 100.0,
        max_y: _BatchFloatType = 1.0,
        min_y: _BatchFloatType = -1.0,
        max_x: _BatchFloatType = 1.0,
        min_x: _BatchFloatType = -1.0,
        scale_xyz=((1.0, 1.0, 1.0),),  # (N, 3)
        R: torch.Tensor = _R,
        T: torch.Tensor = _T,
        K: Optional[torch.Tensor] = None,
        device: Device = "cpu",
    ):
        # 继承父类初始化逻辑
        super().__init__(
            znear=znear,
            zfar=zfar,
            max_y=max_y,
            min_y=min_y,
            max_x=max_x,
            min_x=min_x,
            scale_xyz=scale_xyz,
            R=R,
            T=T,
            K=K,
            device=device,
        )


def erode_torch_batch(binary_img_batch, kernel_size):
    pad = (kernel_size - 1) // 2
    bin_img = F.pad(
        binary_img_batch.unsqueeze(1), pad=[pad, pad, pad, pad], mode="reflect"
    )
    out = -F.max_pool2d(-bin_img, kernel_size=kernel_size, stride=1, padding=0)
    out = out.squeeze(1)
    return out


def dilate_torch_batch(binary_img_batch, kernel_size):
    pad = (kernel_size - 1) // 2
    bin_img = F.pad(binary_img_batch, pad=[pad, pad, pad, pad], mode="reflect")
    out = F.max_pool2d(bin_img, kernel_size=kernel_size, stride=1, padding=0)
    out = out.squeeze()
    return out


# Pytorch3D based renderering functions, managed in a class
# Render size is recommended to be the same as your latent view size
# DO NOT USE "bilinear" sampling when you are handling latents.
# Stable Diffusion has 4 latent channels so use channels=4


class UVProjection:
    def __init__(
        self,
        texture_size=96,
        render_size=64,
        sampling_mode="nearest",
        channels=3,
        device=None,
    ):
        self.channels = channels
        self.device = device or torch.device("cpu")
        self.lights = AmbientLights(
            ambient_color=((1.0,) * channels,), device=self.device
        )
        self.target_size = (texture_size, texture_size)
        self.render_size = render_size
        self.sampling_mode = sampling_mode

    # Load obj mesh, rescale the mesh to fit into the bounding box
    def load_mesh(self, mesh, scale_factor=2.0, auto_center=True, autouv=False):
        if isinstance(mesh, Trimesh):
            vertices = torch.tensor(mesh.vertices, dtype=torch.float32).to(self.device)
            faces = torch.tensor(mesh.faces, dtype=torch.int64).to(self.device)
            mesh = Meshes(verts=[vertices], faces=[faces])
            verts = mesh.verts_packed()
            mesh = mesh.update_padded(verts[None, :, :])
        elif isinstance(mesh, str) and os.path.isfile(mesh):
            mesh = load_objs_as_meshes([mesh_path], device=self.device)
            if auto_center:
                verts = mesh.verts_packed()
                max_bb = (verts - 0).max(0)[0]
                min_bb = (verts - 0).min(0)[0]
                scale = (max_bb - min_bb).max() / 2
                center = (max_bb + min_bb) / 2
                mesh.offset_verts_(-center)
                mesh.scale_verts_((scale_factor / float(scale)))
            else:
                mesh.scale_verts_((scale_factor))

        if autouv or (mesh.textures is None):
            mesh = self.uv_unwrap(mesh)
        self.mesh = mesh

    def load_glb_mesh(
        self, mesh_path, trimesh, scale_factor=1.0, auto_center=True, autouv=False
    ):
        from pytorch3d.io.experimental_gltf_io import MeshGlbFormat

        io = IO()
        io.register_meshes_format(MeshGlbFormat())
        with open(mesh_path, "rb") as f:
            mesh = io.load_mesh(f, include_textures=True, device=self.device)
        if auto_center:
            verts = mesh.verts_packed()

            max_bb = (verts - 0).max(0)[0]
            min_bb = (verts - 0).min(0)[0]
            scale = (max_bb - min_bb).max() / 2
            center = (max_bb + min_bb) / 2
            mesh.offset_verts_(-center)
            mesh.scale_verts_((scale_factor / float(scale)))
            verts = mesh.verts_packed()
            # T = torch.tensor([[1, 0, 0], [0, 0, -1], [0, 1, 0]], device=verts.device, dtype=verts.dtype)
            # T = torch.tensor([[0, 0, 1], [0, 1, 0], [-1, 0, 0]], device=verts.device, dtype=verts.dtype)
            # verts = verts @ T
            mesh = mesh.update_padded(verts[None, :, :])
        else:
            mesh.scale_verts_((scale_factor))
        if autouv or (mesh.textures is None):
            mesh = self.uv_unwrap(mesh)
        self.mesh = mesh

    # Save obj mesh
    def save_mesh(self, mesh_path, texture):
        save_obj(
            mesh_path,
            self.mesh.verts_list()[0],
            self.mesh.faces_list()[0],
            verts_uvs=self.mesh.textures.verts_uvs_list()[0],
            faces_uvs=self.mesh.textures.faces_uvs_list()[0],
            texture_map=texture,
        )

    # Code referred to TEXTure code (https://github.com/TEXTurePaper/TEXTurePaper.git)
    def uv_unwrap(self, mesh):
        verts_list = mesh.verts_list()[0]
        faces_list = mesh.faces_list()[0]

        import xatlas
        import numpy as np

        v_np = verts_list.cpu().numpy()
        f_np = faces_list.int().cpu().numpy()
        atlas = xatlas.Atlas()
        atlas.add_mesh(v_np, f_np)
        chart_options = xatlas.ChartOptions()
        chart_options.max_iterations = 4
        atlas.generate(chart_options=chart_options)
        vmapping, ft_np, vt_np = atlas[0]  # [N], [M, 3], [N, 2]

        vt = (
            torch.from_numpy(vt_np.astype(np.float32))
            .type(verts_list.dtype)
            .to(mesh.device)
        )
        ft = (
            torch.from_numpy(ft_np.astype(np.int64))
            .type(faces_list.dtype)
            .to(mesh.device)
        )

        new_map = torch.zeros(self.target_size + (self.channels,), device=mesh.device)
        new_tex = TexturesUV([new_map], [ft], [vt], sampling_mode=self.sampling_mode)

        mesh.textures = new_tex
        return mesh

    """
		A functions that disconnect faces in the mesh according to
		its UV seams. The number of vertices are made equal to the
		number of unique vertices its UV layout, while the faces list
		is intact.
	"""

    def disconnect_faces(self):
        mesh = self.mesh
        verts_list = mesh.verts_list()
        faces_list = mesh.faces_list()
        verts_uvs_list = mesh.textures.verts_uvs_list()
        faces_uvs_list = mesh.textures.faces_uvs_list()
        packed_list = [v[f] for v, f in zip(verts_list, faces_list)]
        verts_disconnect_list = [
            torch.zeros(
                (verts_uvs_list[i].shape[0], 3),
                dtype=verts_list[0].dtype,
                device=verts_list[0].device,
            )
            for i in range(len(verts_list))
        ]
        for i in range(len(verts_list)):
            verts_disconnect_list[i][faces_uvs_list] = packed_list[i]
        assert not mesh.has_verts_normals(), "Not implemented for vertex normals"
        self.mesh_d = Meshes(verts_disconnect_list, faces_uvs_list, mesh.textures)
        return self.mesh_d

    """
		A function that construct a temp mesh for back-projection.
		Take a disconnected mesh and a rasterizer, the function calculates
		the projected faces as the UV, as use its original UV with pseudo
		z value as world space geometry.
	"""

    def construct_uv_mesh(self):
        mesh = self.mesh_d
        verts_list = mesh.verts_list()
        verts_uvs_list = mesh.textures.verts_uvs_list()
        # faces_list = [torch.flip(faces, [-1]) for faces in mesh.faces_list()]
        new_verts_list = []
        for i, (verts, verts_uv) in enumerate(zip(verts_list, verts_uvs_list)):
            verts = verts.clone()
            verts_uv = verts_uv.clone()
            verts[..., 0:2] = verts_uv[..., :]
            verts = (verts - 0.5) * 2
            verts[..., 2] *= 1
            new_verts_list.append(verts)
        textures_uv = mesh.textures.clone()
        self.mesh_uv = Meshes(new_verts_list, mesh.faces_list(), textures_uv)
        return self.mesh_uv

    # Set texture for the current mesh.
    def set_texture_map(self, texture):
        new_map = texture.permute(1, 2, 0)
        new_map = new_map.to(self.device)
        new_tex = TexturesUV(
            [new_map],
            self.mesh.textures.faces_uvs_padded(),
            self.mesh.textures.verts_uvs_padded(),
            sampling_mode=self.sampling_mode,
        )
        self.mesh.textures = new_tex

    # Set the initial normal noise texture
    # No generator here for replication of the experiment result. Add one as you wish
    def set_noise_texture(self, channels=None):
        if not channels:
            channels = self.channels
        noise_texture = torch.normal(
            0, 1, (channels,) + self.target_size, device=self.device
        )
        self.set_texture_map(noise_texture)
        return noise_texture

    # Set the cameras given the camera poses and centers
    def set_cameras(self, camera_poses, centers=None, camera_distance=2.7, scale=None):
        elev = torch.FloatTensor([pose[0] for pose in camera_poses])
        azim = torch.FloatTensor([pose[1] for pose in camera_poses])
        print("camera_distance:{}".format(camera_distance))
        R, T = look_at_view_transform(
            dist=camera_distance, elev=elev, azim=azim, at=centers or ((0, 0, 0),)
        )
        # flip_mat = torch.from_numpy(np.diag([-1.0, 1.0, -1.0]) ).type(torch.FloatTensor).to(R.device)
        # R = R@flip_mat
        # R = R.permute(0, 2, 1)
        # T = T*torch.from_numpy(np.array([-1.0, 1.0, -1.0])).type(torch.FloatTensor).to(R.device)
        # print("v R size:{}, v T size:{}".format(R.size(), T.size()))
        # c2w = self.get_c2w(elev, [camera_distance]*len(elev), azim)
        # w2c = torch.linalg.inv(c2w)
        # R, T= w2c[:, :3, :3], w2c[:, :3, 3]
        print("R size:{}, T size:{}".format(R.size(), T.size()))
        # self.cameras = CustomOrthographicCameras(device=self.device, R=R, T=T, scale_xyz=scale or ((1,1,1),), znear=0.1, min_x=-0.55, max_x=0.55, min_y=-0.55, max_y=0.55)
        self.cameras = FoVOrthographicCameras(
            device=self.device, R=R, T=T, scale_xyz=scale or ((1, 1, 1),)
        )

    # Set all necessary internal data for rendering and texture baking
    # Can be used to refresh after changing camera positions
    def set_cameras_and_render_settings(
        self,
        camera_poses,
        centers=None,
        camera_distance=2.7,
        render_size=None,
        scale=None,
    ):
        self.set_cameras(camera_poses, centers, camera_distance, scale=scale)
        if render_size is None:
            render_size = self.render_size
        if not hasattr(self, "renderer"):
            self.setup_renderer(size=render_size)
        if not hasattr(self, "mesh_d"):
            self.disconnect_faces()
        if not hasattr(self, "mesh_uv"):
            self.construct_uv_mesh()
        self.calculate_tex_gradient()
        self.calculate_visible_triangle_mask()
        _, _, _, cos_maps, _, _ = self.render_geometry()
        self.calculate_cos_angle_weights(cos_maps)

    # Setup renderers for rendering
    # max faces per bin set to 30000 to avoid overflow in many test cases.
    # You can use default value to let pytorch3d handle that for you.
    def setup_renderer(
        self,
        size=64,
        blur=0.0,
        face_per_pix=1,
        perspective_correct=False,
        channels=None,
    ):
        if not channels:
            channels = self.channels

        self.raster_settings = RasterizationSettings(
            image_size=size,
            blur_radius=blur,
            faces_per_pixel=face_per_pix,
            perspective_correct=perspective_correct,
            cull_backfaces=True,
            max_faces_per_bin=30000,
        )

        self.renderer = MeshRenderer(
            rasterizer=MeshRasterizer(
                cameras=self.cameras,
                raster_settings=self.raster_settings,
            ),
            shader=HardNChannelFlatShader(
                device=self.device,
                cameras=self.cameras,
                lights=self.lights,
                channels=channels,
                # materials=materials
            ),
        )

    # Bake screen-space cosine weights to UV space
    # May be able to reimplement using the generic "bake_texture" function, but it works so leave it here for now
    @torch.enable_grad()
    def calculate_cos_angle_weights(self, cos_angles, fill=True, channels=None):
        if not channels:
            channels = self.channels
        cos_maps = []
        tmp_mesh = self.mesh.clone()
        for i in range(len(self.cameras)):

            zero_map = torch.zeros(
                self.target_size + (channels,), device=self.device, requires_grad=True
            )
            optimizer = torch.optim.SGD([zero_map], lr=1, momentum=0)
            optimizer.zero_grad()
            zero_tex = TexturesUV(
                [zero_map],
                self.mesh.textures.faces_uvs_padded(),
                self.mesh.textures.verts_uvs_padded(),
                sampling_mode=self.sampling_mode,
            )
            tmp_mesh.textures = zero_tex

            images_predicted = self.renderer(
                tmp_mesh, cameras=self.cameras[i], lights=self.lights
            )

            loss = torch.sum((cos_angles[i, :, :, 0:1] ** 1 - images_predicted) ** 2)
            loss.backward()
            optimizer.step()

            if fill:
                zero_map = zero_map.detach() / (self.gradient_maps[i] + 1e-8)
                zero_map = voronoi_solve(
                    zero_map, self.gradient_maps[i][..., 0], self.device
                )
            else:
                zero_map = zero_map.detach() / (self.gradient_maps[i] + 1e-8)
            cos_maps.append(zero_map)
        self.cos_maps = cos_maps

    # Get geometric info from fragment shader
    # Can be used for generating conditioning image and cosine weights
    # Returns some information you may not need, remember to release them for memory saving
    @torch.no_grad()
    def render_geometry(self, image_size=None):
        if image_size:
            size = self.renderer.rasterizer.raster_settings.image_size
            self.renderer.rasterizer.raster_settings.image_size = image_size
        shader = self.renderer.shader
        self.renderer.shader = HardGeometryShader(
            device=self.device, cameras=self.cameras[0], lights=self.lights
        )
        tmp_mesh = self.mesh.clone()

        verts, normals, depths, cos_angles, texels, fragments = self.renderer(
            tmp_mesh.extend(len(self.cameras)), cameras=self.cameras, lights=self.lights
        )
        self.renderer.shader = shader

        if image_size:
            self.renderer.rasterizer.raster_settings.image_size = size

        return verts, normals, depths, cos_angles, texels, fragments

    # Project world normal to view space and normalize
    @torch.no_grad()
    def decode_view_normal(self, normals):
        w2v_mat = self.cameras.get_full_projection_transform()
        normals_view = torch.clone(normals)[:, :, :, 0:3]
        normals_view = normals_view.reshape(normals_view.shape[0], -1, 3)
        normals_view = w2v_mat.transform_normals(normals_view)
        normals_view = normals_view.reshape(normals.shape[0:3] + (3,))
        normals_view[:, :, :, 2] *= -1
        normals = (normals_view[..., 0:3] + 1) * normals[
            ..., 3:
        ] / 2 + torch.FloatTensor(((((0.5, 0.5, 1))))).to(self.device) * (
            1 - normals[..., 3:]
        )
        # normals = torch.cat([normal for normal in normals], dim=1)
        normals = normals.clamp(0, 1)
        return normals

    # Normalize absolute depth to inverse depth
    @torch.no_grad()
    def decode_normalized_depth(self, depths, batched_norm=False):
        view_z, mask = depths.unbind(-1)
        view_z = view_z * mask + 100 * (1 - mask)
        inv_z = 1 / view_z
        inv_z_min = inv_z * mask + 100 * (1 - mask)
        if not batched_norm:
            max_ = torch.max(inv_z, 1, keepdim=True)
            max_ = torch.max(max_[0], 2, keepdim=True)[0]

            min_ = torch.min(inv_z_min, 1, keepdim=True)
            min_ = torch.min(min_[0], 2, keepdim=True)[0]
        else:
            max_ = torch.max(inv_z)
            min_ = torch.min(inv_z_min)
        inv_z = (inv_z - min_) / (max_ - min_)
        inv_z = inv_z.clamp(0, 1)
        inv_z = inv_z[..., None].repeat(1, 1, 1, 3)

        return inv_z

    # Multiple screen pixels could pass gradient to a same texel
    # We can precalculate this gradient strength and use it to normalize gradients when we bake textures
    @torch.enable_grad()
    def calculate_tex_gradient(self, channels=None):
        if not channels:
            channels = self.channels
        tmp_mesh = self.mesh.clone()
        gradient_maps = []
        for i in range(len(self.cameras)):
            zero_map = torch.zeros(
                self.target_size + (channels,), device=self.device, requires_grad=True
            )
            optimizer = torch.optim.SGD([zero_map], lr=1, momentum=0)
            optimizer.zero_grad()
            zero_tex = TexturesUV(
                [zero_map],
                self.mesh.textures.faces_uvs_padded(),
                self.mesh.textures.verts_uvs_padded(),
                sampling_mode=self.sampling_mode,
            )
            tmp_mesh.textures = zero_tex
            images_predicted = self.renderer(
                tmp_mesh, cameras=self.cameras[i], lights=self.lights
            )
            loss = torch.sum((1 - images_predicted) ** 2)
            loss.backward()
            optimizer.step()

            gradient_maps.append(zero_map.detach())

        self.gradient_maps = gradient_maps

    # Get the UV space masks of triangles visible in each view
    # First get face ids from each view, then filter pixels on UV space to generate masks

    @torch.no_grad()
    def get_c2w(
        self,
        elevation_deg: LIST_TYPE,
        distance: LIST_TYPE,
        azimuth_deg: Optional[LIST_TYPE],
        num_views: Optional[int] = 1,
        device: Optional[str] = None,
    ) -> torch.FloatTensor:
        if azimuth_deg is None:
            assert (
                num_views is not None
            ), "num_views must be provided if azimuth_deg is None."
            azimuth_deg = torch.linspace(
                0, 360, num_views + 1, dtype=torch.float32, device=device
            )[:-1]
        else:
            num_views = len(azimuth_deg)

        def list_to_pt(
            x: LIST_TYPE,
            dtype: Optional[torch.dtype] = None,
            device: Optional[str] = None,
        ) -> torch.Tensor:
            if isinstance(x, list) or isinstance(x, np.ndarray):
                return torch.tensor(x, dtype=dtype, device=device)
            return x.to(dtype=dtype)

        azimuth_deg = list_to_pt(azimuth_deg, dtype=torch.float32, device=device)
        elevation_deg = list_to_pt(elevation_deg, dtype=torch.float32, device=device)
        camera_distances = list_to_pt(distance, dtype=torch.float32, device=device)
        elevation = elevation_deg * math.pi / 180
        azimuth = azimuth_deg * math.pi / 180
        camera_positions = torch.stack(
            [
                camera_distances * torch.cos(elevation) * torch.cos(azimuth),
                camera_distances * torch.cos(elevation) * torch.sin(azimuth),
                camera_distances * torch.sin(elevation),
            ],
            dim=-1,
        )
        center = torch.zeros_like(camera_positions)
        up = torch.tensor([0, 0, 1], dtype=torch.float32, device=device)[
            None, :
        ].repeat(num_views, 1)
        lookat = F.normalize(center - camera_positions, dim=-1)
        right = F.normalize(torch.cross(lookat, up, dim=-1), dim=-1)
        up = F.normalize(torch.cross(right, lookat, dim=-1), dim=-1)
        c2w3x4 = torch.cat(
            [torch.stack([right, up, -lookat], dim=-1), camera_positions[:, :, None]],
            dim=-1,
        )
        c2w = torch.cat([c2w3x4, torch.zeros_like(c2w3x4[:, :1])], dim=1)
        c2w[:, 3, 3] = 1.0
        return c2w

    @torch.no_grad()
    def calculate_visible_triangle_mask(self, channels=None, image_size=(512, 512)):
        if not channels:
            channels = self.channels

        pix2face_list = []
        for i in range(len(self.cameras)):
            self.renderer.rasterizer.raster_settings.image_size = image_size
            pix2face = self.renderer.rasterizer(
                self.mesh_d, cameras=self.cameras[i]
            ).pix_to_face
            self.renderer.rasterizer.raster_settings.image_size = self.render_size
            pix2face_list.append(pix2face)

        if not hasattr(self, "mesh_uv"):
            self.construct_uv_mesh()

        raster_settings = RasterizationSettings(
            image_size=self.target_size,
            blur_radius=0,
            faces_per_pixel=1,
            perspective_correct=False,
            cull_backfaces=False,
            max_faces_per_bin=30000,
        )

        R, T = look_at_view_transform(dist=2, elev=0, azim=0)
        # flip_mat = torch.from_numpy(np.diag([-1.0, 1.0, -1.0]) ).type(torch.FloatTensor).to(R.device)
        # R = R@flip_mat
        # T = T*torch.tensor(np.array([-1.0, 1.0, -1.0])).type(torch.FloatTensor).to(R.device)
        # c2w = self.get_c2w([0], [1.8], [0])
        # w2c = torch.linalg.inv(c2w)[:, :3,:]
        # R, T= w2c[:, :3,:3], w2c[:, :3, 3]
        # print("R size:{}, T size:{}".format(R.size(), T.size()))
        cameras = FoVOrthographicCameras(device=self.device, R=R, T=T)
        # cameras = CustomOrthographicCameras(device=self.device, R=R, T=T)

        # cameras = CustomOrthographicCameras(device=self.device, R=R, T=T, znear=0.1, min_x=-0.55, max_x=0.55, min_y=-0.55, max_y=0.55)

        rasterizer = MeshRasterizer(cameras=cameras, raster_settings=raster_settings)
        uv_pix2face = rasterizer(self.mesh_uv).pix_to_face

        visible_triangles = []
        for i in range(len(pix2face_list)):
            valid_faceid = torch.unique(pix2face_list[i])
            valid_faceid = valid_faceid[1:] if valid_faceid[0] == -1 else valid_faceid
            mask = torch.isin(uv_pix2face[0], valid_faceid, assume_unique=False)
            # uv_pix2face[0][~mask] = -1
            triangle_mask = torch.ones(self.target_size + (1,), device=self.device)
            triangle_mask[~mask] = 0

            triangle_mask[:, 1:][triangle_mask[:, :-1] > 0] = 1
            triangle_mask[:, :-1][triangle_mask[:, 1:] > 0] = 1
            triangle_mask[1:, :][triangle_mask[:-1, :] > 0] = 1
            triangle_mask[:-1, :][triangle_mask[1:, :] > 0] = 1
            visible_triangles.append(triangle_mask)

        self.visible_triangles = visible_triangles

    # Render the current mesh and texture from current cameras
    def render_textured_views(self):
        meshes = self.mesh.extend(len(self.cameras))
        images_predicted = self.renderer(
            meshes, cameras=self.cameras, lights=self.lights
        )

        return [image.permute(2, 0, 1) for image in images_predicted]

    @torch.no_grad()
    def get_point_validation_by_o3d(
        self, points, eye_position, hidden_point_removal_radius=200
    ):
        point_visibility = torch.zeros((points.shape[0]), device=points.device).bool()

        pcd = o3d.geometry.PointCloud(
            points=o3d.utility.Vector3dVector(points.cpu().numpy())
        )
        camera_pose = (
            eye_position.get_camera_center().squeeze().cpu().numpy().astype(np.float64)
        )
        # o3d_camera = [0, 0, diameter]
        diameter = np.linalg.norm(
            np.asarray(pcd.get_max_bound()) - np.asarray(pcd.get_min_bound())
        )
        radius = diameter * 200  # The radius of the sperical projection
        _, pt_map = pcd.hidden_point_removal(camera_pose, radius)

        visible_point_ids = np.array(pt_map)

        point_visibility[visible_point_ids] = True
        return point_visibility

    @torch.no_grad()
    def hidden_judge(self, camera, texture_dim):
        mesh = self.mesh

        verts = mesh.verts_packed()
        faces = mesh.faces_packed()
        verts_uv = mesh.textures.verts_uvs_padded()[0]  # 获取打包后的 UV 坐标 (V, 2)
        faces_uv = mesh.textures.faces_uvs_padded()[0]
        uv_face_attr = torch.index_select(
            verts_uv, 0, faces_uv.view(-1)
        )  # 选择对应顶点的 UV 坐标
        uv_face_attr = uv_face_attr.view(
            faces.shape[0], faces_uv.shape[1], 2
        ).unsqueeze(0)
        x, y, z = verts[:, 0], verts[:, 1], verts[:, 2]
        mesh_out_of_range = False
        if (
            x.min() < -1
            or x.max() > 1
            or y.min() < -1
            or y.max() > 1
            or z.min() < -1
            or z.max() > 1
        ):
            mesh_out_of_range = True
        face_vertices_world = kal.ops.mesh.index_vertices_by_faces(
            verts.unsqueeze(0), faces
        )
        face_vertices_z = torch.zeros_like(
            face_vertices_world[:, :, :, -1], device=verts.device
        )
        uv_position, face_idx = kal.render.mesh.rasterize(
            texture_dim,
            texture_dim,
            face_vertices_z,
            uv_face_attr * 2 - 1,
            face_features=face_vertices_world,
        )
        uv_position = torch.clamp(uv_position, -1, 1)
        uv_position[face_idx == -1] = 0

        points = uv_position.reshape(-1, 3)
        mask = points[:, 0] != 0
        valid_points = points[mask]
        # np.save("tmp/pcd.npy", valid_points.cpu().numpy())
        # print(camera.get_camera_center())

        points_visibility = self.get_point_validation_by_o3d(
            valid_points, camera
        ).float()
        visibility_map = torch.zeros((texture_dim * texture_dim,)).to(self.device)
        visibility_map[mask] = points_visibility
        visibility_map = visibility_map.reshape((texture_dim, texture_dim))
        return visibility_map

    @torch.enable_grad()
    def bake_texture(
        self,
        views=None,
        main_views=[],
        cos_weighted=True,
        channels=None,
        exp=None,
        noisy=False,
        generator=None,
        smooth_colorize=False,
    ):
        if not exp:
            exp = 1
        if not channels:
            channels = self.channels
        views = [view.permute(1, 2, 0) for view in views]

        tmp_mesh = self.mesh
        bake_maps = [
            torch.zeros(
                self.target_size + (views[0].shape[2],),
                device=self.device,
                requires_grad=True,
            )
            for view in views
        ]
        optimizer = torch.optim.SGD(bake_maps, lr=1, momentum=0)
        optimizer.zero_grad()
        loss = 0
        for i in range(len(self.cameras)):
            bake_tex = TexturesUV(
                [bake_maps[i]],
                tmp_mesh.textures.faces_uvs_padded(),
                tmp_mesh.textures.verts_uvs_padded(),
                sampling_mode=self.sampling_mode,
            )
            tmp_mesh.textures = bake_tex
            images_predicted = self.renderer(
                tmp_mesh,
                cameras=self.cameras[i],
                lights=self.lights,
                device=self.device,
            )
            predicted_rgb = images_predicted[..., :-1]
            loss += (((predicted_rgb[...] - views[i])) ** 2).sum()
        loss.backward(retain_graph=False)
        optimizer.step()

        total_weights = 0
        baked = 0
        for i in range(len(bake_maps)):
            normalized_baked_map = bake_maps[i].detach() / (
                self.gradient_maps[i] + 1e-8
            )
            bake_map = voronoi_solve(
                normalized_baked_map, self.gradient_maps[i][..., 0], self.device
            )
            # bake_map = voronoi_solve(normalized_baked_map, self.visible_triangles[i].squeeze())

            weight = self.visible_triangles[i] * (self.cos_maps[i]) ** exp
            if smooth_colorize:
                visibility_map = self.hidden_judge(
                    self.cameras[i], self.target_size[0]
                ).unsqueeze(-1)
                weight *= visibility_map
            if noisy:
                noise = (
                    torch.rand(weight.shape[:-1] + (1,), generator=generator)
                    .type(weight.dtype)
                    .to(weight.device)
                )
                weight *= noise
            total_weights += weight

            baked += bake_map * weight
        baked /= total_weights + 1e-8

        whole_visible_mask = None
        if not smooth_colorize:
            baked = voronoi_solve(baked, total_weights[..., 0], self.device)
            tmp_mesh.textures = TexturesUV(
                [baked],
                tmp_mesh.textures.faces_uvs_padded(),
                tmp_mesh.textures.verts_uvs_padded(),
                sampling_mode=self.sampling_mode,
            )
        else:  # smooth colorize
            baked = voronoi_solve(baked, total_weights[..., 0], self.device)
            whole_visible_mask = self.visible_triangles[0].to(torch.int32)
            for tensor in self.visible_triangles[1:]:
                whole_visible_mask = torch.bitwise_or(
                    whole_visible_mask, tensor.to(torch.int32)
                )

            baked *= whole_visible_mask
            tmp_mesh.textures = TexturesUV(
                [baked],
                tmp_mesh.textures.faces_uvs_padded(),
                tmp_mesh.textures.verts_uvs_padded(),
                sampling_mode=self.sampling_mode,
            )

        extended_mesh = tmp_mesh.extend(len(self.cameras))
        images_predicted = self.renderer(
            extended_mesh, cameras=self.cameras, lights=self.lights
        )
        learned_views = [image.permute(2, 0, 1) for image in images_predicted]

        return learned_views, baked.permute(2, 0, 1), total_weights.permute(2, 0, 1)

    # Move the internel data to a specific device
    def to(self, device):
        for mesh_name in ["mesh", "mesh_d", "mesh_uv"]:
            if hasattr(self, mesh_name):
                mesh = getattr(self, mesh_name)
                setattr(self, mesh_name, mesh.to(device))
        for list_name in ["visible_triangles", "visibility_maps", "cos_maps"]:
            if hasattr(self, list_name):
                map_list = getattr(self, list_name)
                for i in range(len(map_list)):
                    map_list[i] = map_list[i].to(device)
