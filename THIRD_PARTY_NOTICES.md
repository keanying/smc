# 第三方声明（THIRD_PARTY_NOTICES）

本项目按根目录 [LICENSE](LICENSE)（SMC 非商业学习许可证 1.0）授权。下列部分来自或参考了第三方项目，
**同时受其原许可证约束**；按这些许可证的要求，在此完整保留其版权声明与许可证文本。

| 第三方项目 | 许可证 | 在本项目中的位置 |
|---|---|---|
| [MediaCrawler](https://github.com/NanmiCoder/MediaCrawler) | NON-COMMERCIAL LEARNING LICENSE 1.1（禁止商用） | 抖音、快手、小红书等采集器的请求参数、签名接入、Cookie 域名等逻辑，见下方文件清单 |
| [ShilongLee/Crawler](https://github.com/ShilongLee/Crawler) | 非商业使用许可证 1.0（禁止商用） | `backend/libs/douyin.cjs`（抖音 `a_bogus` 签名脚本，原样复制） |
| [puppeteer-extra](https://github.com/berstend/puppeteer-extra)（extract-stealth-evasions） | MIT | `backend/libs/stealth.min.js`（自动生成的浏览器指纹脚本，保留原文件头） |
| [xhshow](https://github.com/Cloxl/xhshow) | MIT | 以 pip 依赖引入（`backend/requirements.txt`），代码不在本仓库内 |

参考了 MediaCrawler 逻辑的文件：

- `backend/app/browser/specs.py`
- `backend/app/collectors/douyin.py`
- `backend/app/collectors/kuaishou.py`
- `backend/app/collectors/kuaishou_v2.py`
- `backend/app/collectors/xhs.py`
- `backend/app/core/search_filters.py`
- `backend/record_page.py`
- `backend/test_social_collectors.py`
- `backend/tests/test_end_to_end.py`
- `backend/tests/test_search_filters.py`
- `backend/tests/test_social_collectors.py`
- `backend/verify_record_offline.py`

> 文件头部的许可声明里也逐个标注了上述来源。

---

## MediaCrawler — NON-COMMERCIAL LEARNING LICENSE 1.1

```text
NON-COMMERCIAL LEARNING LICENSE 1.1

Copyright (c) [2024] [relakkes@gmail.com]

WHEREAS:
1. The copyright owner owns and controls the copyright of this software and related documentation files (hereinafter referred to as the "Software");
2. The user wishes to use the Software for learning purposes;
3. The copyright owner is willing to authorize the user to use the Software under the conditions stated in this license;

NOW, THEREFORE, the parties, in compliance with relevant laws and regulations, agree to the following terms:

SCOPE OF AUTHORIZATION:
1. The copyright owner hereby grants any natural person or legal entity (hereinafter referred to as the "User") accepting this license a free, non-exclusive, non-transferable right to use, copy, modify, and merge the Software for non-commercial learning purposes, subject to the following conditions.

CONDITIONS:
1. The User must include the above copyright notice and this license statement in all reasonably prominent locations of the Software and its copies.
2. The Software is limited to learning and research purposes only, and may not be used for large-scale crawling or activities that disrupt platform operations.
3. Without the written consent of the copyright owner, the Software may not be used for any commercial purposes or to cause improper influence on third parties.

DISCLAIMER:
1. The Software is provided "AS IS," without warranty of any kind, express or implied, including but not limited to the warranties of merchantability, fitness for a particular purpose, and non-infringement.
2. In no event shall the copyright owner be liable for any direct, indirect, incidental, special, exemplary, or consequential damages (including, but not limited to, procurement of substitute goods or services; loss of use, data, or profits; or business interruption) however caused and on any theory of liability, whether in contract, strict liability, or tort (including negligence or otherwise) arising in any way out of the use of this Software, even if advised of the possibility of such damage.

APPLICABLE LAW:
1. The interpretation and enforcement of this license shall comply with local laws and regulations.
2. Any disputes arising from or related to this license shall be resolved through friendly negotiation between the parties; if negotiation fails, either party may submit the dispute to the people's court where the copyright owner is located for resolution.

This license constitutes the entire agreement between the parties regarding the Software, superseding and merging all prior discussions, communications, and agreements, whether oral or written.


非商业学习使用许可证 1.1

版权所有 (c) [2024] [relakkes@gmail.com]

鉴于：
1. 版权所有者拥有和控制本软件和相关文档文件（以下简称“软件”）的版权；
2. 使用者希望使用该软件进行学习；
3. 版权所有者愿意在本许可证所述的条件下授权使用者使用该软件；

现因此，双方遵循相关法律法规，同意如下条款：

授权范围：
1. 版权所有者特此免费授予接受本许可证的任何自然人或法人（以下简称“使用者”）非独占的、不可转让的权利，在非商业学习目的下使用、复制、修改、合并本软件，前提是遵守以下条件。

条件：
1. 使用者必须在软件及其副本的所有合理显著位置包含上述版权声明和本许可证声明。
2. 本软件仅限用于学习和研究目的，不得用于大规模爬虫或对平台造成运营干扰的行为。
3. 未经版权所有者书面同意，不得将本软件用于任何商业用途或对第三方造成不当影响。

免责声明：
1. 本软件按“现状”提供，不提供任何形式的明示或暗示保证，包括但不限于对适销性、特定用途的适用性和非侵权的保证。
2. 在任何情况下，版权所有者均不对因使用本软件而产生的，或在任何方式上与本软件有关的任何直接、间接、偶然、特殊、示例性或后果性损害负责（包括但不限于采购替代品或服务；使用、数据或利润的损失；或业务中断），无论这些损害是如何引起的，以及无论是通过合同、严格责任还是侵权行为（包括疏忽或其他方式）产生的，即使已被告知此类损害的可能性。

适用法律：
1. 本许可证的解释和执行应遵循当地法律法规。
2. 因本许可证引起的或与之相关的任何争议，双方应友好协商解决；协商不成时，任何一方可将争议提交至版权所有者所在地的人民法院诉讼解决。

本许可证构成双方之间关于本软件的完整协议，取代并合并以前的讨论、交流和协议，无论是口头还是书面的。
```

---

## ShilongLee/Crawler — 非商业使用许可证 1.0

```text
非商业使用许可证 1.0

版权所有 (c) [2024] [lishilong0101@gmail.com]

鉴于：

1. 版权所有者拥有和控制本软件和相关文档文件（以下简称“软件”）的版权；
2. 使用者希望使用该软件；
3. 版权所有者愿意在本许可证所述的条件下授权使用者使用该软件；

现因此，双方遵循相关法律法规，同意如下条款：

授权范围：

1. 版权所有者特此免费授予接受本许可证的任何自然人或法人（以下简称“使用者”）非独占的、不可转让的权利，在非商业目的下使用、复制、修改、合并本软件，前提是遵守以下条件。

条件：

1. 使用者必须在软件及其副本的所有合理显著位置包含上述版权声明和本许可证声明。
2. 本软件不得用于任何商业目的，包括但不限于销售、营利或商业竞争。
3. 未经版权所有者书面同意，不得将本软件用于任何商业用途。

免责声明：

1. 本软件按“现状”提供，不提供任何形式的明示或暗示保证，包括但不限于对适销性、特定用途的适用性和非侵权的保证。
2. 在任何情况下，版权所有者均不对因使用本软件而产生的，或在任何方式上与本软件有关的任何直接、间接、偶然、特殊、示例性或后果性损害负责（包括但不限于采购替代品或服务；使用、数据或利润的损失；或业务中断），无论这些损害是如何引起的，以及无论是通过合同、严格责任还是侵权行为（包括疏忽或其他方式）产生的，即使已被告知此类损害的可能性。

适用法律：

1. 本许可证的解释和执行应遵循当地法律法规。
2. 因本许可证引起的或与之相关的任何争议，双方应友好协商解决；协商不成时，任何一方可将争议提交至版权所有者所在地的人民法院诉讼解决。

本许可证构成双方之间关于本软件的完整协议，取代并合并以前的讨论、交流和协议，无论是口头还是书面的。
```

---

## puppeteer-extra — MIT License

`backend/libs/stealth.min.js` 由 puppeteer-extra 的 extract-stealth-evasions 生成，文件头注明 `License: MIT`，
版权归 puppeteer-extra 的作者与贡献者所有。MIT 许可证全文见
<https://github.com/berstend/puppeteer-extra/blob/master/LICENSE>。
