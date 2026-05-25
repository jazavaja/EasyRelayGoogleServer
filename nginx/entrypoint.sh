#!/usr/bin/env sh
set -e

# Only substitute these specific variables — leave all $host, $remote_addr etc. alone
envsubst '${EXIT_NODE_PORT} ${EXIT_NODE_MAX_REQUEST_MB}' \
  < /etc/easy-relay-google/nginx/nginx.conf.template \
  > /etc/easy-relay-google/nginx/nginx.conf

exec nginx -g 'daemon off;' -c /etc/easy-relay-google/nginx/nginx.conf