秋窗客流分析账本
运行环境：Python 3.11，已按 requirements.txt 预装依赖。
启动：python -m autumn_ledger serve --database autumn-ledger.sqlite3
接口说明：服务启动后访问 /docs。
合成业务演练：python -B -m autumn_ledger demo
测试：python -B -m unittest discover -s tests -v
构建：python -B -m autumn_ledger.check_source
数据完整性：python -B -m autumn_ledger check --database autumn-ledger.sqlite3
金额单位为人民币分；订单修订为全量快照，退款为累计值。
出游日期是供应商给出的目的地本地日期；事件时间必须含时区。
取消不计出游人数，未退收入仍计入净额。缺报不等于零需求。
构成调整：期间比较按来源×客源城市×工作日/周末分层，以已确认完整的来源城市日期数为暴露，
共同层内冻结基期暴露权重，把强度差拆为层内变化、构成变化与交互部分，逐层贡献可对账。
范围外缺报不阻止限共同层的审核发布，但审核人须确认实际舍弃的范围摘要；
范围内缺报或没有共同暴露仍阻止发布，范围外原始总量仅作内部核对。
本地演练使用虚构数据，分析仅描述所选来源样本。
