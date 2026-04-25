package edu.sub;

import io.grpc.Context;
import io.grpc.Contexts;
import io.grpc.Metadata;
import io.grpc.ServerCall;
import io.grpc.ServerCallHandler;
import io.grpc.ServerInterceptor;

public class SessionIdInterceptor implements ServerInterceptor {

    public static final Context.Key<String> SESSION_ID = Context.key("session-id");
    public static final Metadata.Key<String> META_KEY =
            Metadata.Key.of("session-id", Metadata.ASCII_STRING_MARSHALLER);

    @Override
    public <ReqT, RespT> ServerCall.Listener<ReqT> interceptCall(
            ServerCall<ReqT, RespT> call,
            Metadata headers,
            ServerCallHandler<ReqT, RespT> next) {
        String id = headers.get(META_KEY);
        Context ctx = Context.current().withValue(SESSION_ID, id);
        return Contexts.interceptCall(ctx, call, headers, next);
    }
}
