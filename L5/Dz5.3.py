import string
text = input("Введи рядок для хештегу: ")

words = text.split()
clean_words = []

for word in words:
    clean_word = ""
    for ch in word:
        if ch not in string.punctuation:
            clean_word = clean_word + ch

    if clean_word != "":
        first = clean_word[0].upper()
        rest = clean_word[1:]
        clean_word = first + rest
        clean_words.append(clean_word)

hashtag = "#" + "".join(clean_words)

if len(hashtag) > 140:
    hashtag = hashtag[:140]

print(hashtag)
