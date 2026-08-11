package com.poi.investment.security;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import java.util.List;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpMethod;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.config.Customizer;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.http.SessionCreationPolicy;
import org.springframework.security.core.authority.SimpleGrantedAuthority;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.security.web.SecurityFilterChain;
import org.springframework.security.web.authentication.UsernamePasswordAuthenticationFilter;
import org.springframework.web.cors.CorsConfiguration;
import org.springframework.web.cors.CorsConfigurationSource;
import org.springframework.web.cors.UrlBasedCorsConfigurationSource;
import org.springframework.web.filter.OncePerRequestFilter;

@Configuration
public class SecurityConfig {

  @Bean
  SecurityFilterChain security(HttpSecurity http, JwtService jwt) throws Exception {
    return http
        .csrf(csrf -> csrf.disable())
        .cors(Customizer.withDefaults())
        .sessionManagement(session ->
            session.sessionCreationPolicy(SessionCreationPolicy.STATELESS))
        .authorizeHttpRequests(auth -> auth
            .requestMatchers("/api/v1/health", "/api/v1/auth/login").permitAll()
            .requestMatchers(HttpMethod.POST, "/api/v1/analyses", "/api/v1/bonds").hasAnyRole("ANALYST", "ADMIN")
            .anyRequest().authenticated())
        .addFilterBefore(new JwtFilter(jwt), UsernamePasswordAuthenticationFilter.class)
        .build();
  }

  @Bean
  CorsConfigurationSource corsConfigurationSource(
      @Value("${app.cors-origin}") String configuredOrigin) {
    CorsConfiguration cors = new CorsConfiguration();
    cors.setAllowedOriginPatterns(List.of(
        configuredOrigin,
        "http://localhost:*",
        "http://127.0.0.1:*",
        "http://10.*:*",
        "http://172.*:*",
        "http://192.168.*:*"));
    cors.setAllowedMethods(List.of("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"));
    cors.setAllowedHeaders(List.of("Authorization", "Content-Type", "Accept"));
    cors.setMaxAge(3600L);

    UrlBasedCorsConfigurationSource source = new UrlBasedCorsConfigurationSource();
    source.registerCorsConfiguration("/**", cors);
    return source;
  }

  private static class JwtFilter extends OncePerRequestFilter {
    private final JwtService jwt;

    private JwtFilter(JwtService jwt) {
      this.jwt = jwt;
    }

    @Override
    protected void doFilterInternal(
        HttpServletRequest request,
        HttpServletResponse response,
        FilterChain chain) throws ServletException, IOException {
      String authorization = request.getHeader(HttpHeaders.AUTHORIZATION);
      if (authorization != null && authorization.startsWith("Bearer ")) {
        try {
          JwtService.AppUserPrincipal identity = jwt.identity(authorization.substring(7));
          String username = identity.username();
          var authorities = List.of(new SimpleGrantedAuthority("ROLE_" + identity.role()));
          SecurityContextHolder.getContext().setAuthentication(
              new UsernamePasswordAuthenticationToken(username, null, authorities));
        } catch (Exception ignored) {
          SecurityContextHolder.clearContext();
        }
      }
      chain.doFilter(request, response);
    }
  }
}
