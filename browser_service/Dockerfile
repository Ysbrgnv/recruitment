FROM node:22-bookworm-slim
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright NODE_ENV=production
WORKDIR /app
COPY browser_service/package.json ./
RUN npm install --omit=dev && npx playwright install --with-deps --only-shell chromium && chmod -R a+rX /ms-playwright
COPY browser_service/*.cjs ./
USER node
CMD ["node","container.cjs"]



