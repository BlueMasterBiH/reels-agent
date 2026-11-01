# Reels agent

Automatski objavljuje **trial reelove** na Instagram iz dva Google Drive foldera i javlja ti emailom koji je prešao prag pregleda.

## Kako radi

GitHub Actions pokreće `agent.py` **svaki sat**. Svaki put agent:

1. **Provjerava trial reelove.** Za svaki reel kojem je prošlo 6 sati od objave pročita broj pregleda. Ako ima **≥ 1000**, šalje email na `anel@webmajstor.ba` s linkom. Ti onda u Instagram aplikaciji prebaciš reel na feed. Ako ima manje, dobiješ kratki izvještaj i reel ostaje trial.
   - Instagram API ne dozvoljava da se reel prebaci na feed automatski, pa taj jedan klik ostaje ručni.
2. **Planira objavu.** Objava ide svaki drugi dan. Na dan objave agent odredi najbolji sat ovim redom:
   - aktivnost tvojih pratilaca po satima za taj dan u sedmici (Instagram `online_followers`, zadnjih 30 dana);
   - ako toga nema: pregledi tvojih ranijih reelova prema satu objave;
   - ako ni toga nema: 19:00.

   Vrijeme se uvijek bira između 08:00 i 22:00 po sarajevskom vremenu.
3. **Objavljuje.** Kad dođe planirani sat, agent:
   - uzme sljedeći neobjavljeni video, naizmjenično iz foldera 1 i 2;
   - izvuče kadrove i pošalje ih Claudeu, koji pročita titlove i napiše caption s hookom;
   - kompresuje video ako je veći od 280 MB;
   - objavi ga kao trial reel.

Sve što je objavljeno, s brojem pregleda, bilježi se u `state.json`.

## Podešavanje (jednom)

### 1. Instagram (Meta)
1. Na <https://developers.facebook.com> napravi aplikaciju tipa **Business** i dodaj proizvod **Instagram Graph API** (prijava preko Facebooka).
2. U **Business Settings → Users → System users** napravi system usera i daj mu pristup Facebook stranici i Instagram nalogu.
3. Generiši token za tu aplikaciju s ovim dozvolama:
   - `instagram_basic`
   - `instagram_content_publish`
   - `instagram_manage_insights`
   - `pages_show_list`
   - `pages_read_engagement`
   - `business_management`

   Token system usera ne ističe.
4. Pronađi svoj **IG User ID** pozivom:
   `GET https://graph.facebook.com/v25.0/me/accounts?fields=instagram_business_account&access_token=TOKEN`

### 2. Google Drive
1. U Google Cloud konzoli uključi **Google Drive API** i napravi **service account**, pa preuzmi njegov JSON ključ.
2. Oba foldera podijeli (Viewer) s emailom service accounta (`...@...iam.gserviceaccount.com`).
   - Folderi su u vlasništvu `ivan@motionix.co`. Ako nemaš pravo dijeljenja, zamoli Ivana da ih podijeli ili kopiraj videe u svoj folder.

### 3. Claude API
Na <https://console.anthropic.com> napravi API ključ.

### 4. Email
Za Gmail: uključi 2FA, pa napravi **App password** (Google nalog → Security → App passwords).

### 5. GitHub secrets
U repou otvori **Settings → Secrets and variables → Actions** i dodaj:

| Secret | Vrijednost |
|---|---|
| `IG_ACCESS_TOKEN` | token iz koraka 1 |
| `IG_USER_ID` | Instagram business account ID |
| `GDRIVE_SA_JSON` | cijeli sadržaj JSON ključa service accounta |
| `ANTHROPIC_API_KEY` | Claude API ključ |
| `SMTP_USER` | Gmail adresa s koje se šalje |
| `SMTP_PASSWORD` | App password |

Opcionalno, pod **Variables**:
- `CAPTION_CONTEXT`: kratak opis profila ili brenda (npr. "Webmajstor - digitalni marketing za mala preduzeća u BiH"), da captioni pogode ton.
- `SMTP_HOST` i `SMTP_PORT`: samo ako ne koristiš Gmail.

## Testiranje

U tabu **Actions → Reels agent → Run workflow** možeš ga pokrenuti ručno:
- **dry_run**: skine video i napiše caption (vidi se u logu), ali ne objavljuje i ne šalje email.
- **force_post**: odmah objavi sljedeći video, bez čekanja rasporeda.

## Postavke

Postavke su env varijable u workflowu:

| Varijabla | Zadano | Značenje |
|---|---|---|
| `POST_EVERY_DAYS` | 2 | razmak između objava (dani) |
| `VIEWS_THRESHOLD` | 1000 | prag pregleda |
| `CHECK_AFTER_HOURS` | 6 | nakon koliko sati se provjerava (zbog rasporeda po satu provjera je između 6. i 7. sata) |
| `EARLIEST_HOUR` / `LATEST_HOUR` | 8 / 22 | dozvoljeni sati objave |
| `MAX_UPLOAD_MB` | 280 | veći videi se kompresuju |
