# CloudSense（云听）· Copyright (c) 2026 keanying
# SPDX-License-Identifier: LicenseRef-SMC-NCLL-1.0
# 按《SMC 非商业学习许可证 1.0》授权：仅限学习与研究，禁止任何商业用途，
# 不得用于大规模抓取、绕过平台防护或非法获取个人信息。
# 首次运行即表示已阅读并同意根目录 LICENSE，使用风险与法律责任由使用者自行承担。

"""景区盒子 ↔ 主。

盒子（景区版）：账号、任务、采集都在本地；采到的作品 / 评论**不落本地库**，直接报给主。
主：存储、标注；盒子的数据中心和标注审核页面通过主的 API 查看和修改，只看得到分给它的景区。

    主这边   repo.py      盒子登记（令牌、分到的景区、最近上报）
             gateway.py   认盒子的令牌、限定景区范围、只放行盒子能调的接口
             api：api/boxes.py（盒子管理页）、api/box_api.py（盒子调的接口）
    盒子这边 client.py    调主的接口（上报、查分配）；RemoteDataRepository 给采集用
             forward.py   页面上的数据中心 / 标注请求转给主
             sync.py      定时从主拉分到的景区
    两边共用 codec.py     JSON 里带上日期时间类型
"""
