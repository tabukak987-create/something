FROM maven:3.9-eclipse-temurin-21 AS build
WORKDIR /build
COPY backend-springboot/pom.xml .
RUN mvn -q -DskipTests dependency:go-offline
COPY backend-springboot/src src
RUN mvn -q -DskipTests package

FROM eclipse-temurin:21-jre
WORKDIR /app
RUN useradd --system --create-home poi
COPY --from=build /build/target/poi-api-1.0.0.jar app.jar
USER poi
EXPOSE 8080
ENTRYPOINT ["java", "-jar", "/app/app.jar"]
