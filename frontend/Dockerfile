# 前端镜像：多阶段（node 构建 dist -> nginx 托管 + 反代后端）
FROM node:22-alpine AS build

WORKDIR /app

# 先装依赖，利用层缓存
COPY package.json package-lock.json ./
RUN npm ci

# 再拷源码构建
COPY . .

RUN npm run build


FROM nginx:1.27-alpine AS runtime

# SSE 友好的反代配置
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
