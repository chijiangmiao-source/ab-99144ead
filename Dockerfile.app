# App service: zero-dependency Node.js, no package installation required.
FROM node:20-alpine

ENV NODE_ENV=production
WORKDIR /app

COPY package.json ./
COPY src ./src
COPY public ./public

# Data directory for the durable drill store (mounted as a volume in compose).
RUN mkdir -p /data && chown node:node /data
USER node

EXPOSE 8080
CMD ["node", "src/server.js"]
