FROM node:20-slim

WORKDIR /app

COPY package*.json ./

RUN npm install

RUN addgroup --system appgroup && adduser --system --ingroup appgroup appuser

COPY . .

RUN chown -R appuser:appgroup /app

USER appuser

CMD ["npm", "run", "dev", "--", "--host"]
