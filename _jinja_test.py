#!/usr/bin/env python3
"""测试 Jinja2 模板渲染"""
from jinja2 import Environment
env = Environment()

# 模拟 rendering
tpl = env.from_string('{{ stats.total_tenants if stats else "—" }}')

# 测试 1: stats is None
print(f"stats=None: {tpl.render(stats=None)}")

# 测试 2: stats 是 dict
print(f"stats=dict: {tpl.render(stats={'total_tenants': 2})}")

# 测试 3: stats 在额外的 key 中
print(f"stats in kw: {tpl.render(**{'stats': {'total_tenants': 2}})}")

# 测试 4: 通过 extra dict 解包
extra = {'stats': {'total_tenants': 2}, 'score': 85}
print(f"**extra: {tpl.render(**extra)}")

# 测试 5: extra.pop + **extra
ctx_stats = extra.pop('stats', None)
ctx = {'stats': ctx_stats, **extra}
print(f"pop+**: {tpl.render(**ctx)}")
print(f"ctx_stats={ctx_stats}")

# 测试 6: 空 dict 是 falsy 还是 truthy
tpl2 = env.from_string('{{ "yes" if stats else "no" }}')
print(f"empty dict: {tpl2.render(stats={})}")
print(f"None: {tpl2.render(stats=None)}")
print(f"full dict: {tpl2.render(stats={'a': 1})}")
