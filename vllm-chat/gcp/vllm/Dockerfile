# vLLM server for Cloud Run GPUs. deploy_gcp.sh stages this folder, replaces VLLM_TAG_PLACEHOLDER
# with the version you chose (default in the script), and builds it with Cloud Build.
FROM vllm/vllm-openai:VLLM_TAG_PLACEHOLDER

COPY start_vllm.sh /start_vllm.sh
RUN chmod +x /start_vllm.sh

# The base image's own entrypoint differs between vLLM versions, so use our own.
ENTRYPOINT []
CMD ["/start_vllm.sh"]
