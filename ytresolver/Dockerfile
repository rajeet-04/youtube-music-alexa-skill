FROM node:22-bookworm-slim

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts && npm cache clean --force
COPY resolver.mjs server.mjs ./

USER node
ENV HOST=127.0.0.1 PORT=4417 NODE_ENV=production
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:4417/health').then(r=>process.exit(r.ok?0:1),()=>process.exit(1))"
# Node's permission model: read only the app, never write files or spawn processes.
CMD ["node", "--max-old-space-size=192", "--permission", "--allow-fs-read=/app", "server.mjs"]
