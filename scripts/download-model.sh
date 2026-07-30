#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="./data/models"
HF_BASE="https://huggingface.co/mlx-community"

VOXTRAL_FILES=(
    config.json
    model.safetensors
    model.safetensors.index.json
    params.json
    tekken.json
    voice_embedding/ar_male.safetensors
    voice_embedding/casual_female.safetensors
    voice_embedding/casual_male.safetensors
    voice_embedding/cheerful_female.safetensors
    voice_embedding/de_female.safetensors
    voice_embedding/de_male.safetensors
    voice_embedding/es_female.safetensors
    voice_embedding/es_male.safetensors
    voice_embedding/fr_female.safetensors
    voice_embedding/fr_male.safetensors
    voice_embedding/hi_female.safetensors
    voice_embedding/hi_male.safetensors
    voice_embedding/it_female.safetensors
    voice_embedding/it_male.safetensors
    voice_embedding/neutral_female.safetensors
    voice_embedding/neutral_male.safetensors
    voice_embedding/nl_female.safetensors
    voice_embedding/nl_male.safetensors
    voice_embedding/pt_female.safetensors
    voice_embedding/pt_male.safetensors
)

QWEN3_FILES=(
    config.json
    generation_config.json
    merges.txt
    model.safetensors
    model.safetensors.index.json
    preprocessor_config.json
    tokenizer_config.json
    vocab.json
)

check_downloaded() {
    local dir="$BASE_DIR/$1"
    if [ -f "$dir/model.safetensors" ]; then
        printf "\033[32m[downloaded]\033[0m"
    else
        printf "            "
    fi
}

printf "\033[34mAvailable TTS models:\033[0m\n\n"
printf "  \033[1mvoxtral engine\033[0m (20 voices, 9 languages)\n"
printf "  1. Voxtral-4B-TTS-2603-mlx-4bit   (~2.5 GB, fastest, RTF <1.0x)  %s\n" "$(check_downloaded Voxtral-4B-TTS-2603-mlx-4bit)"
printf "  2. Voxtral-4B-TTS-2603-mlx-6bit   (~3.5 GB, balanced, RTF ~1.1x)  %s\n" "$(check_downloaded Voxtral-4B-TTS-2603-mlx-6bit)"
printf "  3. Voxtral-4B-TTS-2603-mlx-bf16   (~8.0 GB, highest quality, RTF ~6.3x)  %s\n" "$(check_downloaded Voxtral-4B-TTS-2603-mlx-bf16)"
printf "\n"
printf "  \033[1mqwen3 engine\033[0m (9 speakers, 10 languages, emotion via instruct)\n"
printf "  4. Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit  (~3.1 GB, RTF ~0.19)  %s\n" "$(check_downloaded Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit)"
printf "  5. Qwen3-TTS-12Hz-1.7B-CustomVoice-4bit  (~1.8 GB, RTF ~0.19)  %s\n" "$(check_downloaded Qwen3-TTS-12Hz-1.7B-CustomVoice-4bit)"
printf "\n"

read -rp "Select model [1-5]: " choice

case "$choice" in
    1) MODEL_NAME="Voxtral-4B-TTS-2603-mlx-4bit"; ENGINE="voxtral" ;;
    2) MODEL_NAME="Voxtral-4B-TTS-2603-mlx-6bit"; ENGINE="voxtral" ;;
    3) MODEL_NAME="Voxtral-4B-TTS-2603-mlx-bf16"; ENGINE="voxtral" ;;
    4) MODEL_NAME="Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit"; ENGINE="qwen3" ;;
    5) MODEL_NAME="Qwen3-TTS-12Hz-1.7B-CustomVoice-4bit"; ENGINE="qwen3" ;;
    *)
        printf "\033[31mInvalid choice.\033[0m\n"
        exit 1
        ;;
esac

MODEL_DIR="$BASE_DIR/$MODEL_NAME"
BASE_URL="$HF_BASE/$MODEL_NAME/resolve/main"

if [ "$ENGINE" = "voxtral" ]; then
    FILES=("${VOXTRAL_FILES[@]}")
    mkdir -p "$MODEL_DIR/voice_embedding"
else
    FILES=("${QWEN3_FILES[@]}")
    mkdir -p "$MODEL_DIR/speech_tokenizer"
fi

printf "\n\033[34mDownloading %s to %s\033[0m\n\n" "$MODEL_NAME" "$MODEL_DIR"

for file in "${FILES[@]}"; do
    dest="$MODEL_DIR/$file"
    printf "\033[34mDownloading %s...\033[0m\n" "$file"
    curl -L --fail -C - --progress-bar -o "$dest" "$BASE_URL/$file"
done

if [ "$ENGINE" = "qwen3" ]; then
    printf "\n\033[34mDownloading speech_tokenizer files...\033[0m\n"
    for file in config.json configuration.json model.safetensors preprocessor_config.json; do
        dest="$MODEL_DIR/speech_tokenizer/$file"
        printf "\033[34mDownloading speech_tokenizer/%s...\033[0m\n" "$file"
        curl -L --fail -C - --progress-bar -o "$dest" "$BASE_URL/speech_tokenizer/$file"
    done
fi

printf "\n\033[32mDone. Model saved to %s\033[0m\n" "$MODEL_DIR"
printf "\nUpdate config.yaml to:\n"
printf "  engine: %s\n" "$ENGINE"
printf "  model: %s\n" "$MODEL_DIR"
if [ "$ENGINE" = "qwen3" ]; then
    printf "  language: English\n"
    printf "  default_voice: ryan\n"
fi
