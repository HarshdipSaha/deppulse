FROM golang:1.22 AS build
WORKDIR /src
COPY . .
RUN go build -o /app/server .

FROM python:3.12-slim
COPY --from=build /app/server /usr/local/bin/server
CMD ["server"]
