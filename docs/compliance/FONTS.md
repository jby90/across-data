# 自托管字体与多语言导出

仅分发 OFL 开源字体，不打包或嵌入系统字体，不使用字体 CDN。

浏览器使用 Fontsource Inter 5.3.0、Noto Sans SC 5.3.1、Noto Naskh Arabic 5.3.0 的 400/600 WOFF2。上游已经按 unicode-range 分包，本项目不再修改字体字节。完整文件 SHA-256、版本和分包性质见 [browser-fonts-manifest.json](browser-fonts-manifest.json)。CSS 使用 font-display: swap，浏览器按字符范围按需加载。保留各包 OFL 全文，许可页可查看。

PDF 使用 Google Fonts 官方仓库固定提交 `e7db6ca603fd4862010383ff17ffd6cc4ffac1c0` 的未修改 TTF。来源、哈希与许可原件保存在 `backend/app/analysis/fonts/open-fonts-manifest.json`，启动导出前校验文件哈希。

| 字体 | 字节数 | SHA-256 |
|---|---:|---|
| notosanssc | 17772300 | `a3041811a78c361b1de50f953c805e0244951c21c5bd412f7232ef0d899af0da` |
| notonaskharabic | 307592 | `67b5a525a661b607971fbd3f96a81b89d3a768e74534fca84f18ac97e6fab72f` |
| inter | 876576 | `29160a80ff49ddcab2c97711247e08b1fab27a484a329ce8b813d820dc559031` |

PDF 由 WeasyPrint 70 + Pango/HarfBuzz 进行阿拉伯文整形、从右到左排版和字体嵌入；PDF 内部自动子集化，不把生成的子集字体作为独立字体产品发布。所选字体的 OFL 声明中，Noto Sans SC 的保留名称是 Source，Noto/Inter 的所用名称未列作保留名称；版权和 OFL 全文随包保留。Word 只写 Inter / Noto Sans SC / Noto Naskh Arabic 字体名，不嵌入系统字体；接收电脑需安装对应开源字体才能保持相同版式。

旧 SourceHanSansCN-Medium.ttf 的来源尚未重新确认，S12 渲染不再使用或随生产镜像分发，Git 历史保留。字体核验属于工程检查，人工法务确认仍待完成。

Windows 开发验证需要官方 MSYS2 UCRT64 的 Pango 动态库，设置 WEASYPRINT_DLL_DIRECTORIES；Linux 容器安装 Debian 的 libpango-1.0-0、libpangoft2-1.0-0、libharfbuzz-subset0。原生库保持未修改、动态加载，许可证原件留在系统包 copyright 目录。
