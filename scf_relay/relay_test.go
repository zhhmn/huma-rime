package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	cos "github.com/tencentyun/cos-go-sdk-v5"
)

const testToken = "test-relay-token"

func testEvent(payload map[string]any) apiEvent {
	body, err := json.Marshal(payload)
	if err != nil {
		panic(err)
	}
	return apiEvent{
		HTTPMethod: http.MethodPost,
		Headers:    map[string]string{"Authorization": "Bearer " + testToken},
		Body:       string(body),
	}
}

func testPayload() map[string]any {
	return map[string]any{
		"url":           "https://ys-L.ysepan.com/path/changelog.txt",
		"expected_size": 7,
		"cache_key":     "changelog:1",
	}
}

func TestParseRelayRequest(t *testing.T) {
	request, err := parseRelayRequest(testEvent(testPayload()), testToken, 1024)
	if err != nil {
		t.Fatal(err)
	}
	if request.ExpectedSize != 7 {
		t.Fatalf("expected size 7, got %d", request.ExpectedSize)
	}
	if len(strings.TrimPrefix(request.objectKey(), "relay/v1/")) != 64 {
		t.Fatalf("unexpected object key: %s", request.objectKey())
	}
}

func TestParseRelayRequestRejectsInvalidInputs(t *testing.T) {
	tests := []struct {
		name      string
		mutate    func(*apiEvent, map[string]any)
		errorCode string
	}{
		{"wrong token", func(event *apiEvent, _ map[string]any) { event.Headers["Authorization"] = "Bearer wrong" }, "unauthorized"},
		{"non ysepan URL", func(_ *apiEvent, payload map[string]any) { payload["url"] = "https://127.0.0.1/private" }, "invalid_url"},
		{"oversized file", func(_ *apiEvent, payload map[string]any) { payload["expected_size"] = 1025 }, "invalid_size"},
		{"extra field", func(_ *apiEvent, payload map[string]any) { payload["extra"] = true }, "invalid_request"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			payload := testPayload()
			event := testEvent(payload)
			test.mutate(&event, payload)
			if test.name != "wrong token" {
				event = testEvent(payload)
			}
			_, err := parseRelayRequest(event, testToken, 1024)
			var clientErr clientError
			if !errorsAs(err, &clientErr) || clientErr.code != test.errorCode {
				t.Fatalf("expected %s, got %v", test.errorCode, err)
			}
		})
	}
}

func errorsAs(err error, target *clientError) bool {
	value, ok := err.(clientError)
	if ok {
		*target = value
	}
	return ok
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(request *http.Request) (*http.Response, error) {
	return f(request)
}

func responseClient(body string, contentLength int64) *http.Client {
	return &http.Client{Transport: roundTripFunc(func(_ *http.Request) (*http.Response, error) {
		return &http.Response{
			StatusCode:    http.StatusOK,
			Body:          io.NopCloser(strings.NewReader(body)),
			ContentLength: contentLength,
			Header:        http.Header{"Content-Type": []string{"text/plain; charset=utf-8"}},
		}, nil
	})}
}

func TestDownloadSourceHashesExactSize(t *testing.T) {
	path := filepath.Join(t.TempDir(), "download")
	if err := os.WriteFile(path, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	request := relayRequest{"https://ys-L.ysepan.com/file", 7, "changelog:1"}
	result, err := downloadSource(responseClient("content", 7))(context.Background(), request, path)
	if err != nil {
		t.Fatal(err)
	}
	expectedDigest := sha256.Sum256([]byte("content"))
	if result.Size != 7 || result.SHA256 != hex.EncodeToString(expectedDigest[:]) {
		t.Fatalf("unexpected download result: %+v", result)
	}
}

func TestDownloadSourceRejectsSizeMismatch(t *testing.T) {
	path := filepath.Join(t.TempDir(), "download")
	if err := os.WriteFile(path, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	_, err := downloadSource(responseClient("content!", 8))(context.Background(), relayRequest{"https://ys-L.ysepan.com/file", 7, "key"}, path)
	if err == nil || !strings.Contains(err.Error(), "content length mismatch") {
		t.Fatalf("expected content length mismatch, got %v", err)
	}
}

func TestRedirectPolicyRejectsOutsideHost(t *testing.T) {
	client := newDownloadClient()
	request, err := http.NewRequest(http.MethodGet, "https://example.com/file", nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := client.CheckRedirect(request, nil); err == nil {
		t.Fatal("expected outside redirect to fail")
	}
}

type fakeStore struct {
	stored   *storedObject
	putBody  []byte
	putCount int
	signErr  error
}

func (s *fakeStore) Find(_ context.Context, _ string, _ int64) (*storedObject, error) {
	return s.stored, nil
}

func (s *fakeStore) Put(_ context.Context, _ string, path string, downloaded downloadedFile) error {
	body, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	s.putBody = body
	s.putCount++
	s.stored = &storedObject{downloaded.Size, downloaded.SHA256}
	return nil
}

func (s *fakeStore) SignedURL(_ context.Context, key string) (string, error) {
	if s.signErr != nil {
		return "", s.signErr
	}
	return "https://cos.example/" + key + "?signature=hidden", nil
}

func TestRelayReportsSigningStage(t *testing.T) {
	sdkErr := errors.New("GetCredential failed")
	store := &fakeStore{
		stored:  &storedObject{7, strings.Repeat("a", 64)},
		signErr: sdkErr,
	}
	_, err := relay(context.Background(), relayRequest{"https://ys-L.ysepan.com/file", 7, "key"}, store, nil)
	var staged *stageError
	if !errors.As(err, &staged) || staged.stage != stageSign || !errors.Is(err, sdkErr) {
		t.Fatalf("unexpected signing error: %v", err)
	}
}

func TestRelayCacheHitSkipsDownload(t *testing.T) {
	store := &fakeStore{stored: &storedObject{7, strings.Repeat("a", 64)}}
	downloaded := false
	download := func(context.Context, relayRequest, string) (downloadedFile, error) {
		downloaded = true
		return downloadedFile{}, nil
	}
	result, err := relay(context.Background(), relayRequest{"https://ys-L.ysepan.com/file", 7, "key"}, store, download)
	if err != nil {
		t.Fatal(err)
	}
	if downloaded || result["cached"] != true {
		t.Fatalf("unexpected cache-hit result: %+v", result)
	}
}

func TestRelayCacheMissDownloadsAndUploads(t *testing.T) {
	store := &fakeStore{}
	download := func(_ context.Context, _ relayRequest, path string) (downloadedFile, error) {
		if err := os.WriteFile(path, []byte("content"), 0o600); err != nil {
			return downloadedFile{}, err
		}
		return downloadedFile{7, strings.Repeat("b", 64), "text/plain"}, nil
	}
	result, err := relay(context.Background(), relayRequest{"https://ys-L.ysepan.com/file", 7, "key"}, store, download)
	if err != nil {
		t.Fatal(err)
	}
	if result["cached"] != false || store.putCount != 1 || string(store.putBody) != "content" {
		t.Fatalf("unexpected cache-miss result: %+v, store: %+v", result, store)
	}
}

func TestCOSStorePutRetainsFileOwnership(t *testing.T) {
	digest := strings.Repeat("c", 64)
	var uploaded []byte
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		switch request.Method {
		case http.MethodPut:
			var err error
			uploaded, err = io.ReadAll(request.Body)
			if err != nil {
				t.Errorf("read upload: %v", err)
			}
			response.Header().Set("x-cos-hash-crc64ecma", "8883282091546349020")
			response.WriteHeader(http.StatusOK)
		case http.MethodHead:
			response.Header().Set("Content-Length", "7")
			response.Header().Set("x-cos-meta-sha256", digest)
			response.WriteHeader(http.StatusOK)
		default:
			t.Errorf("unexpected method: %s", request.Method)
			response.WriteHeader(http.StatusMethodNotAllowed)
		}
	}))
	defer server.Close()

	bucketURL, err := url.Parse(server.URL)
	if err != nil {
		t.Fatal(err)
	}
	client := cos.NewClient(&cos.BaseURL{BucketURL: bucketURL}, server.Client())
	client.DisableURLCheck()
	store := &cosStore{client: client, signedURLTTL: time.Minute}
	path := filepath.Join(t.TempDir(), "download")
	if err := os.WriteFile(path, []byte("content"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := store.Put(context.Background(), "relay/v1/test", path, downloadedFile{7, digest, "text/plain"}); err != nil {
		t.Fatal(err)
	}
	if string(uploaded) != "content" {
		t.Fatalf("unexpected upload: %q", uploaded)
	}
}
