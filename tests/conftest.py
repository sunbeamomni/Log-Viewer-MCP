from hypothesis import settings

# 每个属性测试默认至少 100 例（design.md「Testing Strategy」）；个别测试可用 @settings 覆盖。
settings.register_profile("log-viewer-mcp", max_examples=100)
settings.load_profile("log-viewer-mcp")
