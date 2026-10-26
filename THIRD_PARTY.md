# 第三方组件

| 组件 | 用途 | 上游 |
|---|---|---|
| ImageHash | pHash / dHash 图像指纹 | https://github.com/JohannesBuchner/imagehash |
| scikit-learn | 邻域搜索、轻量逻辑回归、分组验证 | https://github.com/scikit-learn/scikit-learn |
| OpenCV | SIFT、RANSAC 与清晰度指标 | https://github.com/opencv/opencv |
| timm | 复用 DINOv2 Small 网络和权重加载，Apache-2.0 | https://github.com/huggingface/pytorch-image-models |
| DINOv2 Small 预训练权重 | 冻结的画面特征，Apache-2.0；固定提交和 SHA-256 | https://huggingface.co/timm/vit_small_patch14_dinov2.lvd142m |
| PyTorch / torchvision | 本地 CPU 推理，BSD 风格许可 | https://pytorch.org/ |
| safetensors / huggingface-hub | 安全权重格式及一次性模型安装 | https://huggingface.co/docs |
| Pillow | 图片读取、EXIF 方向、缩略图 | https://github.com/python-pillow/Pillow |
| NumPy / SciPy / PyWavelets | 数值计算及 ImageHash 依赖 | 各项目官方分发 |
| rawpy / LibRaw | RAW 内嵌预览 | https://github.com/letmaik/rawpy |
| PySide6 / Qt | Windows 桌面界面 | https://doc.qt.io/qtforpython-6/ |
| 已安装的 Photoshop / Adobe Camera Raw（可选） | 解释 Adobe XMP 并在临时 RAW 副本上渲染 | https://helpx.adobe.com/ie/photoshop/using/scripting.html |

Python 依赖由安装脚本放入 `local/environment`。各组件的许可证与通知随其官方发行包提供。Photoshop / Camera Raw 为可选的外部渲染软件，由用户另行安装。

DINOv2 权重固定使用上游提交 `4610ca143709d58a633b6397a74412c2c3842454`，SHA-256 为 `04d27f3400d059fc0cfd7d17dd1909a75bf3ea8fb3eeb48b97cb99e57ee20081`。模型以 safetensors 格式加载，安装来源和校验信息记录在 `local/models/dinov2-small/manifest.json`。
