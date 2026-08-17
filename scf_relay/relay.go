package main

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"

	cos "github.com/tencentyun/cos-go-sdk-v5"
)

const (
	userAgent           = "huma-rime-cos-relay/1"
	defaultMaxFileSize  = int64(400 * 1024 * 1024)
	defaultSignedURLTTL = 300 * time.Second
	downloadTimeout     = 120 * time.Second
	copyBufferSize      = 1024 * 1024
)

type apiEvent struct {
	HTTPMethod      string            `json:"httpMethod"`
	Headers         map[string]string `json:"headers"`
	Body            string            `json:"body"`
	IsBase64Encoded bool              `json:"isBase64Encoded"`
	RequestContext  struct {
		HTTPMethod string `json:"httpMethod"`
		HTTP       struct {
			Method string `json:"method"`
		} `json:"http"`
	} `json:"requestContext"`
}

type apiResponse struct {
	IsBase64Encoded bool              `json:"isBase64Encoded"`
	StatusCode      int               `json:"statusCode"`
	Headers         map[string]string `json:"headers"`
	Body            string            `json:"body"`
}

type relayRequest struct {
	SourceURL    string
	ExpectedSize int64
	CacheKey     string
}

func (r relayRequest) objectKey() string {
	identity := fmt.Sprintf("v1\x00%s\x00%d", r.CacheKey, r.ExpectedSize)
	digest := sha256.Sum256([]byte(identity))
	return "relay/v1/" + hex.EncodeToString(digest[:])
}

type storedObject struct {
	Size   int64
	SHA256 string
}

type downloadedFile struct {
	Size        int64
	SHA256      string
	ContentType string
}

type nonClosingReadSeeker struct {
	io.ReadSeeker
}

type objectStore interface {
	Find(context.Context, string, int64) (*storedObject, error)
	Put(context.Context, string, string, downloadedFile) error
	SignedURL(context.Context, string) (string, error)
}

type downloader func(context.Context, relayRequest, string) (downloadedFile, error)

type clientError struct {
	statusCode int
	code       string
}

func (e clientError) Error() string { return e.code }

type failureStage string

const (
	stageCacheLookup failureStage = "cos_cache_lookup"
	stageDownload    failureStage = "source_download"
	stageStore       failureStage = "cos_store"
	stageSign        failureStage = "cos_sign"
)

type stageError struct {
	stage failureStage
	err   error
}

func (e *stageError) Error() string { return string(e.stage) + ": " + e.err.Error() }
func (e *stageError) Unwrap() error { return e.err }

func atStage(stage failureStage, err error) error {
	var staged *stageError
	if errors.As(err, &staged) {
		return err
	}
	return &stageError{stage: stage, err: err}
}

func requestMethod(event apiEvent) string {
	for _, method := range []string{
		event.HTTPMethod,
		event.RequestContext.HTTPMethod,
		event.RequestContext.HTTP.Method,
	} {
		if method != "" {
			return strings.ToUpper(method)
		}
	}
	return ""
}

func authorizationHeader(headers map[string]string) string {
	for name, value := range headers {
		if strings.EqualFold(name, "Authorization") {
			return value
		}
	}
	return ""
}

func parseRelayRequest(event apiEvent, token string, maxFileSize int64) (relayRequest, error) {
	if requestMethod(event) != http.MethodPost {
		return relayRequest{}, clientError{http.StatusMethodNotAllowed, "method_not_allowed"}
	}
	expectedAuthorization := "Bearer " + token
	if !hmac.Equal([]byte(authorizationHeader(event.Headers)), []byte(expectedAuthorization)) {
		return relayRequest{}, clientError{http.StatusUnauthorized, "unauthorized"}
	}

	body := []byte(event.Body)
	if event.IsBase64Encoded {
		decoded, err := base64.StdEncoding.Strict().DecodeString(event.Body)
		if err != nil {
			return relayRequest{}, clientError{http.StatusBadRequest, "invalid_json"}
		}
		body = decoded
	}

	var fields map[string]json.RawMessage
	if err := decodeOneJSON(body, &fields); err != nil {
		return relayRequest{}, clientError{http.StatusBadRequest, "invalid_json"}
	}
	if len(fields) != 3 || fields["url"] == nil || fields["expected_size"] == nil || fields["cache_key"] == nil {
		return relayRequest{}, clientError{http.StatusBadRequest, "invalid_request"}
	}

	var request relayRequest
	if err := json.Unmarshal(fields["url"], &request.SourceURL); err != nil || !allowedSourceURL(request.SourceURL) {
		return relayRequest{}, clientError{http.StatusBadRequest, "invalid_url"}
	}
	if err := json.Unmarshal(fields["expected_size"], &request.ExpectedSize); err != nil || request.ExpectedSize <= 0 || request.ExpectedSize > maxFileSize {
		return relayRequest{}, clientError{http.StatusBadRequest, "invalid_size"}
	}
	if err := json.Unmarshal(fields["cache_key"], &request.CacheKey); err != nil || request.CacheKey == "" || len([]byte(request.CacheKey)) > 512 {
		return relayRequest{}, clientError{http.StatusBadRequest, "invalid_cache_key"}
	}
	return request, nil
}

func decodeOneJSON(data []byte, destination any) error {
	decoder := json.NewDecoder(strings.NewReader(string(data)))
	if err := decoder.Decode(destination); err != nil {
		return err
	}
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		if err == nil {
			return errors.New("multiple JSON values")
		}
		return err
	}
	return nil
}

func allowedSourceURL(value string) bool {
	parsed, err := url.ParseRequestURI(value)
	if err != nil || parsed.Scheme != "https" || parsed.User != nil || parsed.Fragment != "" || parsed.Opaque != "" {
		return false
	}
	host := strings.ToLower(parsed.Hostname())
	if host != "ysepan.com" && !strings.HasSuffix(host, ".ysepan.com") {
		return false
	}
	return parsed.Port() == "" || parsed.Port() == "443"
}

func newDownloadClient() *http.Client {
	return &http.Client{
		Timeout: downloadTimeout,
		CheckRedirect: func(request *http.Request, via []*http.Request) error {
			if len(via) >= 10 {
				return errors.New("too many upstream redirects")
			}
			if !allowedSourceURL(request.URL.String()) {
				return errors.New("upstream redirected outside the allowlist")
			}
			return nil
		},
	}
}

func downloadSource(client *http.Client) downloader {
	return func(ctx context.Context, request relayRequest, destination string) (downloadedFile, error) {
		upstreamRequest, err := http.NewRequestWithContext(ctx, http.MethodGet, request.SourceURL, nil)
		if err != nil {
			return downloadedFile{}, fmt.Errorf("create upstream request: %w", err)
		}
		upstreamRequest.Header.Set("User-Agent", userAgent)
		response, err := client.Do(upstreamRequest)
		if err != nil {
			return downloadedFile{}, fmt.Errorf("download upstream: %w", err)
		}
		defer response.Body.Close()
		if response.StatusCode < 200 || response.StatusCode >= 300 {
			return downloadedFile{}, fmt.Errorf("upstream status: %d", response.StatusCode)
		}
		if response.ContentLength >= 0 && response.ContentLength != request.ExpectedSize {
			return downloadedFile{}, errors.New("upstream content length mismatch")
		}

		output, err := os.OpenFile(destination, os.O_WRONLY|os.O_TRUNC, 0o600)
		if err != nil {
			return downloadedFile{}, fmt.Errorf("open temporary file: %w", err)
		}
		digest := sha256.New()
		limited := io.LimitReader(response.Body, request.ExpectedSize+1)
		buffer := make([]byte, copyBufferSize)
		size, copyErr := io.CopyBuffer(io.MultiWriter(output, digest), limited, buffer)
		closeErr := output.Close()
		if copyErr != nil {
			return downloadedFile{}, fmt.Errorf("read upstream: %w", copyErr)
		}
		if closeErr != nil {
			return downloadedFile{}, fmt.Errorf("close temporary file: %w", closeErr)
		}
		if size != request.ExpectedSize {
			return downloadedFile{}, errors.New("upstream response size mismatch")
		}
		contentType := response.Header.Get("Content-Type")
		if contentType == "" {
			contentType = "application/octet-stream"
		}
		return downloadedFile{size, hex.EncodeToString(digest.Sum(nil)), contentType}, nil
	}
}

func relay(ctx context.Context, request relayRequest, store objectStore, download downloader) (map[string]any, error) {
	key := request.objectKey()
	stored, err := store.Find(ctx, key, request.ExpectedSize)
	if err != nil {
		return nil, atStage(stageCacheLookup, err)
	}
	cached := stored != nil
	if stored == nil {
		temporary, err := os.CreateTemp("/tmp", "huma-rime-relay-")
		if err != nil {
			return nil, fmt.Errorf("create temporary file: %w", err)
		}
		temporaryPath := temporary.Name()
		if err := temporary.Close(); err != nil {
			os.Remove(temporaryPath)
			return nil, fmt.Errorf("close temporary file: %w", err)
		}
		defer os.Remove(temporaryPath)

		downloaded, err := download(ctx, request, temporaryPath)
		if err != nil {
			return nil, atStage(stageDownload, err)
		}
		if err := store.Put(ctx, key, temporaryPath, downloaded); err != nil {
			return nil, atStage(stageStore, err)
		}
		stored = &storedObject{downloaded.Size, downloaded.SHA256}
	}
	signedURL, err := store.SignedURL(ctx, key)
	if err != nil {
		return nil, atStage(stageSign, err)
	}
	return map[string]any{
		"url":    signedURL,
		"size":   stored.Size,
		"sha256": stored.SHA256,
		"cached": cached,
	}, nil
}

type cosStore struct {
	client       *cos.Client
	signedURLTTL time.Duration
}

func (s *cosStore) Find(ctx context.Context, key string, expectedSize int64) (*storedObject, error) {
	response, err := s.client.Object.Head(ctx, key, nil)
	if cos.IsNotFoundError(err) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	size, err := strconv.ParseInt(response.Header.Get("Content-Length"), 10, 64)
	if err != nil {
		return nil, fmt.Errorf("parse COS object size: %w", err)
	}
	digest := response.Header.Get("x-cos-meta-sha256")
	if size != expectedSize || len(digest) != 64 {
		return nil, nil
	}
	return &storedObject{size, digest}, nil
}

func (s *cosStore) Put(ctx context.Context, key, path string, downloaded downloadedFile) error {
	body, err := os.Open(path)
	if err != nil {
		return err
	}
	metadata := http.Header{}
	metadata.Set("x-cos-meta-expected-size", strconv.FormatInt(downloaded.Size, 10))
	metadata.Set("x-cos-meta-sha256", downloaded.SHA256)
	_, putErr := s.client.Object.Put(ctx, key, nonClosingReadSeeker{body}, &cos.ObjectPutOptions{
		ObjectPutHeaderOptions: &cos.ObjectPutHeaderOptions{
			ContentType:      downloaded.ContentType,
			ContentLength:    downloaded.Size,
			XCosMetaXXX:      &metadata,
			XCosStorageClass: "STANDARD",
		},
	})
	closeErr := body.Close()
	if putErr != nil {
		return putErr
	}
	if closeErr != nil {
		return closeErr
	}
	stored, err := s.Find(ctx, key, downloaded.Size)
	if err == nil && stored != nil && stored.SHA256 == downloaded.SHA256 {
		return nil
	}
	_, _ = s.client.Object.Delete(ctx, key)
	if err != nil {
		return fmt.Errorf("verify COS object: %w", err)
	}
	return errors.New("COS object verification failed")
}

func (s *cosStore) SignedURL(ctx context.Context, key string) (string, error) {
	presignedURL, err := s.client.Object.GetPresignedURL2(ctx, http.MethodGet, key, s.signedURLTTL, nil)
	if err != nil {
		return "", err
	}
	return presignedURL.String(), nil
}

func storeFromEnvironment() (*cosStore, error) {
	bucket := os.Getenv("COS_BUCKET")
	region := os.Getenv("COS_REGION")
	secretID := os.Getenv("TENCENTCLOUD_SECRETID")
	secretKey := os.Getenv("TENCENTCLOUD_SECRETKEY")
	sessionToken := os.Getenv("TENCENTCLOUD_SESSIONTOKEN")
	if bucket == "" || region == "" || secretID == "" || secretKey == "" || sessionToken == "" {
		return nil, errors.New("incomplete COS environment")
	}
	bucketURL, err := cos.NewBucketURL(bucket, region, true)
	if err != nil {
		return nil, fmt.Errorf("construct COS URL: %w", err)
	}
	transport := &cos.AuthorizationTransport{
		SecretID:     secretID,
		SecretKey:    secretKey,
		SessionToken: sessionToken,
	}
	client := cos.NewClient(&cos.BaseURL{BucketURL: bucketURL}, &http.Client{Transport: transport})
	ttl, err := durationFromEnvironment("SIGNED_URL_TTL", defaultSignedURLTTL)
	if err != nil {
		return nil, err
	}
	return &cosStore{client, ttl}, nil
}

func durationFromEnvironment(name string, fallback time.Duration) (time.Duration, error) {
	value := os.Getenv(name)
	if value == "" {
		return fallback, nil
	}
	seconds, err := strconv.ParseInt(value, 10, 64)
	if err != nil || seconds <= 0 {
		return 0, fmt.Errorf("invalid %s", name)
	}
	return time.Duration(seconds) * time.Second, nil
}

func maxFileSizeFromEnvironment() (int64, error) {
	value := os.Getenv("MAX_FILE_SIZE")
	if value == "" {
		return defaultMaxFileSize, nil
	}
	size, err := strconv.ParseInt(value, 10, 64)
	if err != nil || size <= 0 {
		return 0, errors.New("invalid MAX_FILE_SIZE")
	}
	return size, nil
}

func jsonResponse(statusCode int, payload any) apiResponse {
	body, err := json.Marshal(payload)
	if err != nil {
		panic(err)
	}
	return apiResponse{
		StatusCode: statusCode,
		Headers:    map[string]string{"Content-Type": "application/json"},
		Body:       string(body),
	}
}

func logRelayError(err error) {
	entry := struct {
		Stage   string `json:"error_stage"`
		Type    string `json:"error_type"`
		Message string `json:"error_message,omitempty"`
	}{
		Stage: "internal",
		Type:  fmt.Sprintf("%T", err),
	}
	var staged *stageError
	if errors.As(err, &staged) {
		entry.Stage = string(staged.stage)
		entry.Type = fmt.Sprintf("%T", staged.err)
		if staged.stage == stageSign {
			entry.Message = staged.err.Error()
		}
	}
	encoded, marshalErr := json.Marshal(entry)
	if marshalErr != nil {
		log.Printf(`{"error_stage":"logging"}`)
		return
	}
	log.Print(string(encoded))
}

func handleRequest(ctx context.Context, event apiEvent) (apiResponse, error) {
	token := os.Getenv("RELAY_TOKEN")
	maxFileSize, configErr := maxFileSizeFromEnvironment()
	if token == "" || configErr != nil {
		log.Printf(`{"error_type":"configuration"}`)
		return jsonResponse(http.StatusBadGateway, map[string]string{"error": "relay_failed"}), nil
	}
	request, err := parseRelayRequest(event, token, maxFileSize)
	if err != nil {
		var clientErr clientError
		if errors.As(err, &clientErr) {
			return jsonResponse(clientErr.statusCode, map[string]string{"error": clientErr.code}), nil
		}
		logRelayError(err)
		return jsonResponse(http.StatusBadGateway, map[string]string{"error": "relay_failed"}), nil
	}
	store, err := storeFromEnvironment()
	if err == nil {
		var result map[string]any
		result, err = relay(ctx, request, store, downloadSource(newDownloadClient()))
		if err == nil {
			log.Printf(`{"cache_key_sha256":%q,"size":%d,"cached":%t}`,
				strings.TrimPrefix(request.objectKey(), "relay/v1/"), result["size"], result["cached"])
			return jsonResponse(http.StatusOK, result), nil
		}
	}
	logRelayError(err)
	return jsonResponse(http.StatusBadGateway, map[string]string{"error": "relay_failed"}), nil
}
