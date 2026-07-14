pipeline {
    agent { label 'jenkins-agent' }

    triggers {
        githubPush()
    }

    environment {
        // ──────── Source Code ────────
        GIT_URL         = 'https://github.com/devsjayanth/Hello-App.git'
        GIT_CRED        = 'github-cred'

        // ──────── Quality ────────
        SONAR_CRED      = 'sonar-cred'

        // ──────── Docker ────────
        DOCKERHUB_USER  = 'devsjayanth'
        DOCKERHUB_CRED  = 'dockerhub-cred'
        IMAGE_NAME      = "${DOCKERHUB_USER}/${APP_NAME}"
        IMAGE_TAG       = "${BUILD_NUMBER}"
        IMAGE_LATEST    = "latest"

        // ──────── Application ────────
        APP_NAME           = 'hello-app-mongo'
        APP_CONTAINER_PORT = '8000'

        // ──────── Dev Server ────────
        DEV_SERVER         = '10.0.2.154'
        DEV_SERVER_CRED    = 'dev-server-cred'
        DEV_APP_PORT       = '9001'

        // ──────── GitOps / ArgoCD ────────
        GITOPS_REPO     = 'https://github.com/devsjayanth/Hello-App-Mongo-GitOps.git'
        GITOPS_BRANCH   = 'main'
        GITOPS_CRED     = 'github-cred'
        MANIFEST_PATH   = 'k8s/hello-app-deployment.yml'
        INGRESS_PATH    = 'k8s/hello-app-ingress.yml'
        LB_IP           = 'REPLACE_WITH_HAPROXY_EIP'
    }

    stages {
        stage('Clean Workspace') {
            steps {
                cleanWs()
            }
        }

        stage('Checkout') {
            steps {
                checkout([
                    $class: 'GitSCM',
                    branches: [[name: '*/main']],
                    userRemoteConfigs: [[
                        url: GIT_URL,
                        credentialsId: GIT_CRED
                    ]]
                ])
            }
        }

        stage('SonarQube Analysis') {
            steps {
                withSonarQubeEnv(installationName: 'sonar-scanner', credentialsId: SONAR_CRED) {
                    sh 'sonar-scanner -Dsonar.projectKey=${APP_NAME} -Dsonar.sources=. -Dsonar.qualitygate.wait=true'
                }
            }
        }

        stage('Build') {
            steps {
                script {
                    docker.build("${IMAGE_NAME}:${IMAGE_TAG}", "--no-cache --pull .")
                    sh "docker tag ${IMAGE_NAME}:${IMAGE_TAG} ${IMAGE_NAME}:${IMAGE_LATEST}"
                }
            }
        }

        stage('Trivy Scan') {
            steps {
                sh "trivy image --exit-code 1 --severity HIGH,CRITICAL --ignore-unfixed ${IMAGE_NAME}:${IMAGE_TAG}"
            }
        }

        stage('Push to DockerHub') {
            steps {
                withCredentials([
                    usernamePassword(
                        credentialsId: DOCKERHUB_CRED,
                        usernameVariable: 'DOCKERHUB_USER',
                        passwordVariable: 'DOCKERHUB_PASS'
                    )
                ]) {
                    sh "echo \$DOCKERHUB_PASS | docker login -u \$DOCKERHUB_USER --password-stdin"
                    sh "docker push ${IMAGE_NAME}:${IMAGE_TAG}"
                    sh "docker push ${IMAGE_NAME}:${IMAGE_LATEST}"
                }
            }
        }

        stage('Deploy to Dev Server') {
            steps {
                withCredentials([sshUserPrivateKey(
                    credentialsId: DEV_SERVER_CRED,
                    keyFileVariable: 'SSH_KEY',
                    usernameVariable: 'SSH_USER'
                )]) {
                    sh """
                        ssh -i \$SSH_KEY -o StrictHostKeyChecking=no \$SSH_USER@${DEV_SERVER} '
                            set -e
                            docker stop hello-app 2>/dev/null || true
                            docker rm hello-app 2>/dev/null || true

                            docker run -d --name hello-app --restart unless-stopped --init \
                              -p ${DEV_APP_PORT}:${APP_CONTAINER_PORT} \
                              -e PORT=${APP_CONTAINER_PORT} -e LOG_LEVEL=INFO -e DEBUG=false -e FORWARDED_ALLOW_IPS=* \
                              ${IMAGE_NAME}:${IMAGE_TAG}

                            docker image prune -f
                        '"""
                }
            }
        }

        stage('Health Check') {
            steps {
                withCredentials([sshUserPrivateKey(
                    credentialsId: DEV_SERVER_CRED,
                    keyFileVariable: 'SSH_KEY',
                    usernameVariable: 'SSH_USER'
                )]) {
                    sh "sleep 5 && ssh -i \$SSH_KEY -o StrictHostKeyChecking=no \$SSH_USER@${DEV_SERVER} curl -sf http://127.0.0.1:${DEV_APP_PORT}/"
                }
            }
        }

        stage('Approve for Production') {
            steps {
                input message: 'Deploy to K8s via ArgoCD?', ok: 'Yes, deploy to production'
            }
        }

        stage('Push to GitOps Repo') {
            steps {
                withCredentials([usernamePassword(
                    credentialsId: GITOPS_CRED,
                    usernameVariable: 'GIT_USER',
                    passwordVariable: 'GIT_PASS'
                )]) {
                    script {
                        def encodedPass = java.net.URLEncoder.encode(GIT_PASS, "UTF-8")
                        def repoUrl = GITOPS_REPO.replaceFirst(/^https?:\/\//, "https://${GIT_USER}:${encodedPass}@")
                        sh """
                            rm -rf gitops
                            git clone ${repoUrl} gitops
                            cd gitops
                            sed -i 's|image:.*|image: ${IMAGE_NAME}:${IMAGE_TAG}|' ${MANIFEST_PATH}
                            sed -i 's|host: .*|host: \"${LB_IP}\"|' ${INGRESS_PATH}
                            git config user.name 'Jenkins'
                            git config user.email 'jenkins@devops'
                            git add ${MANIFEST_PATH} ${INGRESS_PATH}
                            git commit -m 'deploy: bump ${APP_NAME} to ${IMAGE_TAG}'
                            git push
                        """
                    }
                }
            }
        }
    }
}
