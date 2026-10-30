package cn.pppyl.lingxiworkspace;

import okhttp3.*;
import org.json.JSONArray;
import java.io.IOException;
import java.util.Arrays;
import java.util.Collections;
import java.util.concurrent.TimeUnit;

/** Android's native TLS trust store, HTTP/2 and streaming response bodies. */
public final class NativeHttp {
    private static final OkHttpClient base = new OkHttpClient.Builder().followRedirects(false).followSslRedirects(false).retryOnConnectionFailure(false).build();
    public static Result request(String method, String url, String headerJson, byte[] body,
                                 boolean http2, int connectSeconds, int readSeconds) throws Exception {
        OkHttpClient client = base.newBuilder()
                .protocols(http2 ? Arrays.asList(Protocol.HTTP_2, Protocol.HTTP_1_1) : Collections.singletonList(Protocol.HTTP_1_1))
                .connectTimeout(connectSeconds, TimeUnit.SECONDS).readTimeout(readSeconds, TimeUnit.SECONDS).build();
        Request.Builder builder = new Request.Builder().url(url);
        JSONArray headers = new JSONArray(headerJson);
        for (int i = 0; i < headers.length(); i++) {
            JSONArray pair = headers.getJSONArray(i);
            builder.addHeader(pair.getString(0), pair.getString(1));
        }
        RequestBody payload = (method.equals("GET") || method.equals("HEAD")) ? null : RequestBody.create(body, null);
        return new Result(client.newCall(builder.method(method, payload).build()).execute());
    }
    public static final class Result {
        private final Response response;
        Result(Response value) { response = value; }
        public int status() { return response.code(); }
        public String headers() throws Exception {
            JSONArray result = new JSONArray();
            for (int i = 0; i < response.headers().size(); i++) {
                JSONArray pair = new JSONArray(); pair.put(response.headers().name(i)); pair.put(response.headers().value(i)); result.put(pair);
            }
            return result.toString();
        }
        public byte[] read() throws IOException {
            if (response.body().source().exhausted()) return null;
            return response.body().source().readByteArray(Math.min(response.body().source().buffer().size(), 8192));
        }
        public void close() { response.close(); }
    }
}
