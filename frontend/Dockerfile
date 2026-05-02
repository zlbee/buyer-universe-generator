# syntax=docker/dockerfile:1.7

FROM node:22-alpine

WORKDIR /app

COPY package*.json ./
# Cache npm's package tarballs between builds while keeping node_modules clean.
RUN --mount=type=cache,target=/root/.npm npm ci

COPY . .

EXPOSE 5173

CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
