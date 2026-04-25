package edu.sub;

import io.grpc.Server;
import io.grpc.ServerInterceptors;
import io.grpc.netty.shaded.io.grpc.netty.NettyServerBuilder;

import java.io.FileInputStream;
import java.io.IOException;
import java.util.Properties;
import java.util.concurrent.TimeUnit;

public class Main {

    public static void main(String[] args) throws Exception {
        String configPath = args.length > 0 ? args[0] : "config.properties";
        Properties cfg = loadConfig(configPath);
        int port = Integer.parseInt(cfg.getProperty("port", "50051"));
        long tickMs = (long) (Double.parseDouble(cfg.getProperty("tick_seconds", "1.0")) * 1000);
        int ttl = Integer.parseInt(cfg.getProperty("session_ttl_seconds", "60"));
        int bufCap = Integer.parseInt(cfg.getProperty("buffer_cap", "1000"));
        int keepAlive = Integer.parseInt(cfg.getProperty("keepalive_seconds", "20"));

        Sessions sessions = new Sessions(ttl, bufCap);
        CanServiceImpl service = new CanServiceImpl(sessions);

        Thread generator = new Thread(() -> {
            while (!Thread.currentThread().isInterrupted()) {
                try {
                    Thread.sleep(tickMs);
                } catch (InterruptedException e) {
                    return;
                }
                var update = Catalog.generateRandom();
                sessions.recordStats(update);
                sessions.dispatch(update);
            }
        }, "generator");
        generator.setDaemon(true);
        generator.start();

        Thread purger = new Thread(() -> {
            while (!Thread.currentThread().isInterrupted()) {
                try {
                    Thread.sleep(5000);
                } catch (InterruptedException e) {
                    return;
                }
                int n = sessions.purgeExpired();
                if (n > 0) System.out.println("[purger] removed expired sessions: " + n);
            }
        }, "purger");
        purger.setDaemon(true);
        purger.start();

        Server server = NettyServerBuilder.forPort(port)
                .keepAliveTime(keepAlive, TimeUnit.SECONDS)
                .keepAliveTimeout(10, TimeUnit.SECONDS)
                .permitKeepAliveTime(5, TimeUnit.SECONDS)
                .permitKeepAliveWithoutCalls(true)
                .addService(ServerInterceptors.intercept(service, new SessionIdInterceptor()))
                .build();

        server.start();
        System.out.println("[main] listening on port " + port
                + " tick=" + tickMs + "ms ttl=" + ttl + "s bufCap=" + bufCap
                + " keepAlive=" + keepAlive + "s");

        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            System.out.println("[main] shutting down");
            server.shutdown();
        }));

        server.awaitTermination();
    }

    private static Properties loadConfig(String path) {
        Properties p = new Properties();
        try (FileInputStream f = new FileInputStream(path)) {
            p.load(f);
            System.out.println("[main] loaded config from " + path);
        } catch (IOException e) {
            System.out.println("[main] " + path + " not found, using defaults");
        }
        return p;
    }
}
