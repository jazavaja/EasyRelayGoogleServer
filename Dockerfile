FROM python:3.12-slim

WORKDIR /app

COPY vps_exit_node.py .

# کاربر non-root برای امنیت
RUN useradd -r -s /bin/false exitnode
USER exitnode

EXPOSE 8181

# PSK از env میخونه، پورت از آرگومان
CMD ["sh", "-c", "python vps_exit_node.py --port ${EXIT_NODE_PORT:-8181}"]