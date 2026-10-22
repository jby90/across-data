# 字体登记与使用规则

## 原则

1. **不使用系统字体做分发/嵌入**:微软雅黑、宋体、黑体、苹方、Arial、Segoe UI、方正、汉仪等均无嵌入/分发授权,不得打包、不得嵌入平台生成的 PDF。
2. 只使用 **OFL-1.1 / Apache-2.0** 开源字体,**自托管**,不引用 Google Fonts 等外部 CDN。
3. 阿拉伯文必须使用带 HarfBuzz 整形的渲染链(WeasyPrint/浏览器),并支持从右到左。
4. **子集化、转换格式(如 TTF→WOFF2)视为修改**:核对保留字体名(RFN)条款,保留版权声明与许可全文,必要时改名。
5. Word 导出只写字体名,**不分发系统字体**;需要版式完全一致时嵌入下表中的开源字体。
6. 新增字体必须先在本表登记,再入库。

## 登记表

| 字体 | 用途 | 许可 | 版本 | 来源 | SHA-256 | 子集化 | 状态 |
|---|---|---|---|---|---|---|---|
| 思源黑体 CN Medium(Source Han Sans CN) | 中文界面/报告正文 | Apache-2.0(随字体文件内嵌声明,2014 版) | 待登记 | 一期使用的文件,SHA-256 见 `AutoDataCross/.../FONT-NOTICE.json`:`fa660569…8644b43`(TTF) | 见左 | 否 | **待重新下载并核对** |
| Noto Naskh Arabic Regular | 阿拉伯文正文 | OFL-1.1 | 2.021 | https://github.com/notofonts/arabic | `8a515763…92689840` | 否 | 待迁入 |
| Noto Sans SC / Noto Serif SC | 中文界面/报告衬线 | OFL-1.1 | 待登记 | https://github.com/notofonts/noto-cjk | 待登记 | 计划按 unicode-range 分包 | 待选定 |
| Inter | 拉丁/法文界面 | OFL-1.1 | 待登记 | https://github.com/rsms/inter | 待登记 | 否 | 待选定 |
| JetBrains Mono | 等宽(条号、哈希) | OFL-1.1 | 待登记 | https://github.com/JetBrains/JetBrainsMono | 待登记 | 否 | 待选定 |

> 上表"待登记"项在字体入库时用 `Get-FileHash -Algorithm SHA256` 填写,并核对来源页面上的许可证文本。
> 一期的 Source Han Sans 文件标注为 Apache-2.0(Adobe 2014 版本)。新平台入库前要重新从官方来源下载并核对版本与许可文本,**不直接拷贝一期仓库文件**。

## 待办

- [ ] 选定中文字体版本(Source Han Sans / Noto Sans SC)并下载入库,填 SHA-256。
- [ ] 为 WeasyPrint PDF 导出准备字体目录与 fontconfig 配置。
- [ ] 前端字体分包与 `font-display` 策略。
- [ ] 评估是否需要中文衬线字体用于报告。
