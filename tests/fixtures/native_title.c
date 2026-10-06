#include <stdio.h>
#include <string.h>
int main(int argc, char **argv) {
 if (argc != 2) return 1;
 FILE *commands = fopen(argv[1], "r");
 if (!commands) return 2;
 char line[512];
 puts("Terminal TLDR isolated verification fixture — not a real agent");
 while (fgets(line, sizeof line, commands)) {
  line[strcspn(line, "\r\n")] = 0;
  printf("\033]0;%s\007", line);
  fflush(stdout);
 }
 fclose(commands);
 return 0;
}
