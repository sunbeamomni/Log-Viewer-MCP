# 测试运行容器：以非 root 用户（uid 10001）运行全部 pytest。构建上下文为仓库根目录。
# Python 3.11 是 pyproject 要求的最低版本；镜像自带 GNU grep，供 validate.py 判断扩展正则。
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 先只装依赖（基础依赖加 server、test 两组 extras 的锁定版本），源码改动时不必重装
COPY pyproject.toml /tmp/pyproject.toml
RUN python -c "import tomllib; p = tomllib.load(open('/tmp/pyproject.toml', 'rb'))['project']; e = p['optional-dependencies']; print('\n'.join(sorted({*p['dependencies'], *(r for k in ('server', 'test') for r in e[k])})))" >/tmp/requirements.txt \
 && pip install -r /tmp/requirements.txt \
 && rm /tmp/pyproject.toml /tmp/requirements.txt

RUN groupadd -g 10001 tester \
 && useradd -u 10001 -g 10001 -m -s /bin/bash tester

# 生产镜像的集成测试（test_image_smoke.py）：该服务以 root 运行，审计文件为 0600 root。
# tester 只能经 sudo 不带参数运行 it-image-audit（读取只读挂载的 /image-audit），没有其他 sudo 权限。
COPY tests/integration/docker/runner/it-image-audit /usr/local/bin/it-image-audit
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends sudo \
 && rm -rf /var/lib/apt/lists/* \
 && sed -i 's/\r$//' /usr/local/bin/it-image-audit \
 && chown root:root /usr/local/bin/it-image-audit \
 && chmod 0755 /usr/local/bin/it-image-audit \
 && echo 'tester ALL=(root) NOPASSWD: /usr/local/bin/it-image-audit ""' >/etc/sudoers.d/it-image-audit \
 && chmod 0440 /etc/sudoers.d/it-image-audit \
 && visudo -cf /etc/sudoers.d/it-image-audit

COPY --chown=10001:10001 . /work
WORKDIR /work
# 装入包本身（不带依赖），使 [project.scripts] 的入口点登记在包元数据中；导入仍经 PYTHONPATH 取 /work 下的源码
RUN pip install --no-deps /work \
 && rm -rf /work/build /work/log_viewer_mcp.egg-info
ENV PYTHONPATH=/work
USER 10001:10001

CMD ["python", "-m", "pytest"]
