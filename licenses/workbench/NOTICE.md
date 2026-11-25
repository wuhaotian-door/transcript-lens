# 本地模型来源

- 规章检索使用 BAAI bge-small-zh-v1.5，原模型标注 MIT 许可。推理采用 Xenova 发布的固定 ONNX int8 转换版。[原模型](https://huggingface.co/BAAI/bge-small-zh-v1.5) [ONNX 转换版](https://huggingface.co/Xenova/bge-small-zh-v1.5/tree/75c43b069aac4d136ba6bc1122f995fedcfd2781)
- 照片匹配使用 OpenCV Zoo YuNet 和 SFace int8，各自的许可证见此目录的 YuNet-LICENSE.txt 和 SFace-LICENSE.txt。[固定模型版本](https://huggingface.co/opencv/opencv_zoo/tree/d4938dfc9d4ec5d098bfa33e98b3f3345a236586)
- 模型权重由用户主动安装。下载和离线导入均校验其完整性。
